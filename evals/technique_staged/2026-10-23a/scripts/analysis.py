"""Gold v3 labelling round: agreement of the strong labellers and luna's scores against them (no LLM call).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-23a/scripts/analysis.py score <GOLD_V3_DIR> [--resamples N]
    uv run --no-sync python evals/technique_staged/2026-10-23a/scripts/analysis.py blindness <GOLD_V3_DIR> \
        <TRANSCRIPTS_DIR> <SHARED_TMP_DIR>

``GOLD_V3_DIR`` is the private labelling directory (``packets.py``'s OUT): ``sessions/<task>-<labeller>-r<k>/out/``,
``luna/<task>/<item_id>.json`` (luna's eval rows), ``sampling/sample.jsonl``, ``docs/``. ``score`` writes
``analysis/`` there (``units-<task>-<unit>.jsonl``, ``metrics.json``, ``tables.md``); nothing is written elsewhere.

**Runs.** Six strong runs per task: opus, sonnet, sol x r1, r2 (``RUNS``); three families. A run file whose
``labeller`` or ``item_id`` differs from its session is not that run's label (e.g. a file another session's script
wrote): it is reported and the run counts as missing on that document; consensus metrics use only documents where
all six runs are present (``complete`` documents). luna: the eval row's payload (an ``ok`` row with no rows is the
empty prediction; a failed row too).

**Units** (what one label is), per document:

* techniques, ``slugs``: technique slug presence, group nodes left out (the strict primary metric of
  ``stack/evals/technique_staged.py``); labellers' ``alt_slugs`` and ``level_alternatives`` are ignored (as the
  strict metric does; the ``tolerant`` lines use ``tolerant_counts`` with the single run as gold); there are no
  ``dont_care`` rows in labeller output. ``cells2`` / ``cells1``: (slug, role) at strength >= 2 / >= 1, group nodes
  out.
* model_roles, ``cells2`` / ``cells1``: (node, role) at strength >= 2 / >= 1 of the ``models`` rows (the 2026-10-17a
  scorer, ``score.model_roles_block``); a labeller row with ``refined_node_id`` grades that node, luna's ``node_id``
  is already the refined node. ``nodes``: nodes with any role >= 1. ``mentions``: the labellers' ``mentions`` node ids
  (luna has none; the LLM-free matcher's strong set from ``sampling/mentions.jsonl`` stands in).

**Support** of a unit = the number of the six runs that have it (1-6); ``>=k/6`` consensus = units with support
>= k. Per unit the labellers' attributes: modal confidence of the supporting runs (ties to the higher), and for slug
/ node presence the *engagement* = median over supporting runs of the strongest role other than ``mentioned`` and
``cited_work`` (0 = mention or citation only) and the *primary role* = the role (other than those two) given by most
supporting runs (ties: higher summed strength, then name).

**Metrics.** P / R / F1 micro over documents (sums of tp, fp, fn), unweighted and weighted by the sampling design
weight (``sample.jsonl`` ``weight``: corpus-level estimates over the screen-relevant frame). Agreement between two
runs: micro Jaccard, Dice (= F1 of one against the other) and Cohen's kappa over the closed universe (techniques: the
234 slugs, x 12 roles for cells; model_roles: each document's menu nodes and every node a run graded, x 10 roles;
none for mentions). Leave-one-family-out (LOFO): the reference is built from the four runs of the other two
families (``>=k/4``), and the left-out family's two runs and luna are scored against the same reference. Bootstrap:
documents resampled with replacement within source kind (the sampling strata), ``--resamples`` (default 2,000,
seed ``SEED``), 95 % percentile intervals.

``arms`` scores eval runs of luna arms (prompt or configuration variants) against the same references: per arm
P / R / F1 against >= 4/6, recall on 6/6 units and by support, precision against the union, failures, schema
degrades and cost per 1,000 documents (rows' ``standalone_cost_usd`` / ``call_cost_usd``: the arm's cost without
cache reuse across runs), with bootstrap intervals and paired differences against a baseline arm. ``--arm
LABEL=RUN[,RUN...]`` (a run directory of ``stack eval``; several, or one run with ``--repeat`` > 1, are pooled
replicates); ``RUN@OVERLAY`` replaces the items an overlay run has (a run over a subset of items, e.g. the long
documents only). It writes ``analysis/arms-<task>.md`` and ``.json`` and nothing else.

``blindness`` scans the Claude Code labeller transcripts (``TRANSCRIPTS_DIR``: ``wf_*/agent-*.jsonl``) for paths
outside each agent's session directory, for shared temporary files that several agents wrote or ran, and compares
the six runs' files for identical content (whole files, rationale, row quote + reason / note); it prints a report
and writes nothing.
"""
from __future__ import annotations

import argparse
import itertools
import json
import random
import re
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path.cwd()))
from stack.evals.technique_staged import taxonomy_structure, tolerant_counts

Row = dict[str, Any]
FAMILIES = ("opus", "sonnet", "sol")
RUNS = tuple(f"{f}-r{k}" for f in FAMILIES for k in (1, 2))
TASKS = ("techniques", "model_roles")
TAXONOMY = Path("taxonomy/taxonomy-v0.9.1.yaml")
TECH_ROLES = ("mentioned", "cited_work", "discussed", "used", "evaluated", "modified", "introduced", "analyzed",
              "attested_use", "suspected_use", "considered", "critiqued")
MR_ROLES = ("mentioned", "cited_work", "discussed", "usage_experience", "built_on", "instrument", "modified",
            "evaluated_behavioral", "analyzed_internals", "introduced")
SHALLOW = ("mentioned", "cited_work")
CONF_RANK = {"high": 3, "med": 2, "low": 1, None: 0}
TRUNCATED = ("g3-0051", "g3-0100")  # over the production 300k-character window: luna saw the first 300k
SEED = 20261023
UNITS = {"techniques": ("slugs", "cells2", "cells1"), "model_roles": ("cells2", "cells1", "nodes", "mentions")}

Units = dict[str, Row]  # unit key (json-encoded) -> attributes {conf, strength, roles}


def family(run: str) -> str:
    return run.split("-")[0]


def read_jsonl(path: Path) -> list[Row]:
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


# ── loading and normalisation ────────────────────────────────────────────────────────────────


def load_labels(root: Path, task: str, items: Sequence[str], accept_foreign: bool = False
                ) -> tuple[dict[str, dict[str, Row]], list[str]]:
    """``run -> item -> label`` and the problems (missing or foreign files; ``accept_foreign``: a foreign file
    counts as the run's label, for the sensitivity line)."""
    out: dict[str, dict[str, Row]] = {r: {} for r in RUNS}
    problems = []
    for run in RUNS:
        d = root / "sessions" / f"{task}-{run}" / "out"
        for iid in items:
            f = d / f"{iid}.json"
            if not f.exists():
                problems.append(f"{task}-{run}/{iid}: missing")
                continue
            x = json.loads(f.read_text())
            if x.get("labeller") != run or x.get("item_id") != iid:
                problems.append(f"{task}-{run}/{iid}: labeller {x.get('labeller')!r} item {x.get('item_id')!r}"
                                " - not this run's label, counted as missing")
                if not accept_foreign:
                    continue
            out[run][iid] = x
    return out, problems


def load_luna(root: Path, task: str, items: Sequence[str]) -> tuple[dict[str, Row], Counter]:
    out, status = {}, Counter()
    for iid in items:
        r = json.loads((root / "luna" / task / f"{iid}.json").read_text())
        status[r["status"]] += 1
        out[iid] = (r.get("payload") or {}) if r["status"] == "ok" else {}
    return out, status


def _merge(units: Units, key: Any, conf: Any, strength: int, roles: Mapping[str, int] | None = None) -> None:
    k = json.dumps(key)
    e = units.setdefault(k, {"conf": conf, "strength": 0, "roles": {}})
    if CONF_RANK.get(conf, 0) > CONF_RANK.get(e["conf"], 0):
        e["conf"] = conf
    e["strength"] = max(e["strength"], strength)
    for r, v in (roles or {}).items():
        e["roles"][r] = max(e["roles"].get(r, 0), v)


def tech_units(label: Row, unit: str, groups: frozenset[str]) -> Units:
    out: Units = {}
    for t in label.get("techniques") or []:
        s = str(t["slug"])
        if s in groups:
            continue
        roles = {str(r): int(v) for r, v in (t.get("roles") or {}).items() if v}
        conf = t.get("confidence")
        if unit == "slugs":
            _merge(out, s, conf, max(roles.values(), default=0), roles)
        else:
            t_min = 2 if unit == "cells2" else 1
            for r, v in roles.items():
                if v >= t_min:
                    _merge(out, [s, r], conf, v)
    return out


def mr_units(label: Row, unit: str, luna: bool) -> Units:
    out: Units = {}
    if unit == "mentions":
        for m in label.get("mentions") or []:
            _merge(out, str(m["node_id"]), None, 1)
        return out
    for m in label.get("models") or []:
        node = str(m["node_id"] if luna else (m.get("refined_node_id") or m["node_id"]))
        roles = {str(r): int(v) for r, v in (m.get("roles") or {}).items() if v}
        conf = m.get("confidence")
        if unit == "nodes":
            if roles:
                _merge(out, node, conf, max(roles.values()), roles)
        else:
            t_min = 2 if unit == "cells2" else 1
            for r, v in roles.items():
                if v >= t_min:
                    _merge(out, [node, r], conf, v)
    return out


# ── metrics ──────────────────────────────────────────────────────────────────────────────────


def prf(tp: float, fp: float, fn: float) -> Row:
    p = tp / (tp + fp) if tp + fp else None
    r = tp / (tp + fn) if tp + fn else None
    f = (2 * p * r / (p + r) if p + r else 0.0) if p is not None and r is not None else None
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f}


def counts(pred: Iterable[str], ref: Iterable[str]) -> tuple[int, int, int]:
    p, g = set(pred), set(ref)
    return len(p & g), len(p - g), len(g - p)


def summed(per_doc: Mapping[str, tuple[float, ...]], docs: Iterable[str], w: Mapping[str, float] | None = None
           ) -> list[float]:
    acc: list[float] = []
    for d in docs:
        x = per_doc[d]
        if not acc:
            acc = [0.0] * len(x)
        k = w[d] if w else 1.0
        for i, v in enumerate(x):
            acc[i] += k * v
    return acc


def kappa(a: float, b: float, c: float, n: float) -> float | None:
    """Cohen's kappa of two binary raters over n units: a both, b first only, c second only."""
    if n <= 0:
        return None
    d = n - a - b - c
    po = (a + d) / n
    p1, p2 = (a + b) / n, (a + c) / n
    pe = p1 * p2 + (1 - p1) * (1 - p2)
    return None if pe >= 1 else (po - pe) / (1 - pe)


def fmt(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def ci_fmt(x: float | None, lo: float | None, hi: float | None) -> str:
    return f"{fmt(x)} [{fmt(lo)}, {fmt(hi)}]"


def table(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return out + [""]


class Analysis:
    """One task and unit type: the runs' and luna's units per document, supports and every metric."""

    def __init__(self, task: str, unit: str, runs: Mapping[str, Mapping[str, Units]], luna: Mapping[str, Units] | None,
                 docs: Sequence[str], universe: Mapping[str, int] | None, meta: Mapping[str, Row]):
        self.task, self.unit, self.runs, self.luna, self.docs, self.universe, self.meta = (
            task, unit, runs, luna, list(docs), universe, meta)
        self.w = {d: float(meta[d]["weight"]) for d in self.docs}
        self.support: dict[str, Counter] = {d: Counter(k for r in RUNS for k in runs[r][d]) for d in self.docs}

    # references
    def ref(self, d: str, k: int, among: Sequence[str] = RUNS) -> set[str]:
        c = Counter(key for r in among for key in self.runs[r][d])
        return {key for key, n in c.items() if n >= k}

    def per_doc(self, pred: Callable[[str], Iterable[str]], ref: Callable[[str], Iterable[str]]
                ) -> dict[str, tuple[int, int, int]]:
        return {d: counts(pred(d), ref(d)) for d in self.docs}

    def score(self, pd: Mapping[str, tuple[int, int, int]], docs: Sequence[str] | None = None, weighted: bool = False
              ) -> Row:
        return prf(*summed(pd, docs or self.docs, self.w if weighted else None))

    def luna_keys(self, d: str) -> set[str]:
        return set(self.luna[d]) if self.luna is not None else set()

    # agreement
    def agreement(self, a: str, b: str) -> Row:
        tp, fp, fn = summed(self.per_doc(lambda d: self.runs[a][d], lambda d: self.runs[b][d]), self.docs)
        out = {"jaccard": tp / (tp + fp + fn) if tp + fp + fn else None,
               "dice": 2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None, "kappa": None,
               "mean_doc_jaccard": statistics.mean(
                   [(len(set(self.runs[a][d]) & set(self.runs[b][d])) / len(set(self.runs[a][d]) | set(self.runs[b][d])))
                    if (self.runs[a][d] or self.runs[b][d]) else 1.0 for d in self.docs])}
        if self.universe is not None:
            out["kappa"] = kappa(tp, fp, fn, sum(self.universe[d] for d in self.docs))
        return out


def unit_attrs(an: Analysis, d: str, key: str) -> Row:
    """Labeller-side attributes of one unit on one document (module docstring)."""
    sup = [r for r in RUNS if key in an.runs[r][d]]
    confs = Counter(an.runs[r][d][key]["conf"] for r in sup)
    conf = max(confs, key=lambda c: (confs[c], CONF_RANK.get(c, 0))) if sup else None
    out: Row = {"support": len(sup), "runs": sup, "families": len({family(r) for r in sup}), "conf": conf,
                "luna": key in an.luna_keys(d)}
    if an.unit == "mentions":
        return out
    if an.unit in ("slugs", "nodes"):
        deep = [max([v for r_, v in an.runs[r][d][key]["roles"].items() if r_ not in SHALLOW], default=0) for r in sup]
        out["engagement"] = statistics.median(deep) if deep else 0
        votes: Counter = Counter()
        tot: Counter = Counter()
        for r in sup:
            for r_, v in an.runs[r][d][key]["roles"].items():
                if r_ not in SHALLOW and v:
                    votes[r_] += 1
                    tot[r_] += v
        out["primary_role"] = (min(votes, key=lambda x: (-votes[x], -tot[x], x)) if votes else "mention/cite only")
    else:
        out["role"] = json.loads(key)[1]
        out["median_strength"] = statistics.median(an.runs[r][d][key]["strength"] for r in sup) if sup else 0
    return out


def engagement_band(e: float) -> str:
    return "0 (mention/cite)" if e < 1 else ("1" if e < 2 else ("2" if e < 3 else "3-4"))


# ── bootstrap ────────────────────────────────────────────────────────────────────────────────


class Boot:
    """Document bootstrap within source kind; metrics are functions of summed per-document count tuples."""

    def __init__(self, docs: Sequence[str], meta: Mapping[str, Row], resamples: int):
        self.docs = list(docs)
        by: dict[str, list[str]] = defaultdict(list)
        for d in self.docs:
            by[meta[d]["source_kind"]].append(d)
        rng = random.Random(SEED)
        self.draws = [[rng.choice(v) for v in by.values() for _ in v] for _ in range(resamples)]

    def ci(self, stat: Callable[[Sequence[str]], float | None]) -> tuple[float | None, float | None]:
        vals = sorted(v for v in (stat(s) for s in self.draws) if v is not None)
        if not vals:
            return None, None
        return vals[int(0.025 * (len(vals) - 1))], vals[int(0.975 * (len(vals) - 1))]


# ── report ───────────────────────────────────────────────────────────────────────────────────


def analyse(an: Analysis, boot: Boot | None, md: list[str], metrics: Row, extra_runs: Row) -> None:
    name = f"{an.task}/{an.unit}"
    md.append(f"## {name} ({len(an.docs)} documents)\n")
    m: Row = metrics.setdefault(name, {})
    has_luna = an.luna is not None
    pred_label = "luna" if an.unit != "mentions" else "matcher (strong)"

    # sizes
    md += table(["run", "units", "per doc"], [[r, sum(len(an.runs[r][d]) for d in an.docs),
                                                fmt(sum(len(an.runs[r][d]) for d in an.docs) / len(an.docs), 2)]
                                               for r in RUNS] +
                ([[pred_label, sum(len(an.luna_keys(d)) for d in an.docs),
                   fmt(sum(len(an.luna_keys(d)) for d in an.docs) / len(an.docs), 2)]] if has_luna else []))

    # (a) agreement
    rows, m["agreement"] = [], {}
    for a, b in itertools.combinations(RUNS + (("luna",) if has_luna else ()), 2):
        if "luna" in (a, b):
            continue
        ag = an.agreement(a, b)
        m["agreement"][f"{a}|{b}"] = ag
        rows.append([f"{a} vs {b}", "repeat" if family(a) == family(b) else "between", fmt(ag["jaccard"]),
                     fmt(ag["mean_doc_jaccard"]), fmt(ag["dice"]), fmt(ag["kappa"])])
    if has_luna:
        for r in RUNS:
            tp, fp, fn = summed(an.per_doc(an.luna_keys, lambda d, r=r: an.runs[r][d]), an.docs)
            n = sum(an.universe[d] for d in an.docs) if an.universe else 0
            rows.append([f"{pred_label} vs {r}", "luna", fmt(tp / (tp + fp + fn) if tp + fp + fn else None), "",
                         fmt(2 * tp / (2 * tp + fp + fn) if tp + fp + fn else None),
                         fmt(kappa(tp, fp, fn, n) if an.universe else None)])
    md.append("**Agreement** (micro Jaccard, mean per-document Jaccard, Dice = F1, Cohen's kappa over the closed "
              "universe)\n")
    md += table(["pair", "kind", "Jaccard", "doc Jaccard", "Dice", "kappa"], rows)
    fam_rows = []
    for kind_, pairs in (("repeat", [(f"{f}-r1", f"{f}-r2") for f in FAMILIES]),
                         ("between", [(a, b) for a, b in itertools.combinations(RUNS, 2) if family(a) != family(b)])):
        for p in pairs if kind_ == "repeat" else [None]:
            sel = [p] if p else pairs
            ags = [m["agreement"][f"{a}|{b}"] for a, b in sel]
            fam_rows.append([(f"{p[0].split('-')[0]} r1 vs r2" if p else "between families (mean of 12 pairs)"),
                             fmt(statistics.mean(x["jaccard"] for x in ags)),
                             fmt(statistics.mean(x["dice"] for x in ags)),
                             fmt(statistics.mean(x["kappa"] for x in ags) if an.universe else None)])
    md += table(["summary", "Jaccard", "Dice", "kappa"], fam_rows)

    # (b) support distribution
    attrs = {(d, k): unit_attrs(an, d, k) for d in an.docs for k in an.support[d]}
    m["units"] = len(attrs)
    rows = []
    for s in range(1, 7):
        xs = [a for a in attrs.values() if a["support"] == s]
        hi = sum(a["conf"] == "high" for a in xs)
        eng = sum(a.get("engagement", a.get("median_strength", 0)) >= 2 for a in xs)
        fam = Counter(a["families"] for a in xs)
        row = [s, len(xs), fmt(len(xs) / len(attrs) if attrs else None), fmt(hi / len(xs) if xs else None),
               fmt(eng / len(xs) if xs else None), f"{fam[1]}/{fam[2]}/{fam[3]}"]
        if has_luna:
            row.append(fmt(sum(a["luna"] for a in xs) / len(xs) if xs else None))
        rows.append(row)
    m["support_hist"] = {str(s): sum(a["support"] == s for a in attrs.values()) for s in range(1, 7)}
    md.append("**Support** (how many of the 6 strong runs have the unit; modal confidence high; engagement / median "
              "strength >= 2; families 1/2/3; luna's recall)\n")
    md += table(["support", "units", "share", "conf high", "engaged >= 2", "families 1/2/3"] +
                ([f"{pred_label} recall"] if has_luna else []), rows)
    # per-run share of own units that are singletons (fickleness per labeller)
    rows = []
    for r in RUNS:
        own = [(d, k) for d in an.docs for k in an.runs[r][d]]
        rows.append([r, len(own), fmt(sum(attrs[x]["support"] == 1 for x in own) / len(own) if own else None),
                     fmt(sum(attrs[x]["support"] >= 4 for x in own) / len(own) if own else None)])
    md += table(["run", "units", "share support 1 (only this run)", "share support >= 4"], rows)
    # by stratum
    rows = []
    for kind_ in ("paper", "forum", "blog", "card", "report"):
        ds = {d for d in an.docs if an.meta[d]["source_kind"] == kind_}
        xs = [a for (d, _), a in attrs.items() if d in ds]
        h = Counter(a["support"] for a in xs)
        rows.append([kind_, len(ds), len(xs), " / ".join(str(h[s]) for s in range(1, 7)),
                     fmt(sum(a["support"] >= 4 for a in xs) / len(xs) if xs else None)])
    md += table(["source kind", "docs", "units", "support 1/2/3/4/5/6", "share >= 4"], rows)
    if has_luna:
        rows = []
        for fams in (1, 2, 3):
            for runs_n in sorted({a["support"] for a in attrs.values() if a["families"] == fams}):
                xs = [a for a in attrs.values() if a["families"] == fams and a["support"] == runs_n]
                rows.append([fams, runs_n, len(xs), fmt(sum(a["luna"] for a in xs) / len(xs))])
        md.append(f"**{pred_label} recall by families and runs supporting the unit**\n")
        md += table(["families", "runs", "units", f"{pred_label} recall"], rows)

    if not has_luna:
        return

    # (c) luna vs references
    refs: dict[str, Callable[[str], set[str]]] = {}
    for r in RUNS:
        refs[r] = lambda d, r=r: set(an.runs[r][d])
    for k in (1, 2, 3, 4, 5, 6):
        refs[f">={k}/6"] = lambda d, k=k: an.ref(d, k)
    rows, m["luna_vs"] = [], {}
    pds = {name_: an.per_doc(an.luna_keys, f) for name_, f in refs.items()}
    pooled = {d: tuple(map(sum, zip(*[pds[r][d] for r in RUNS]))) for d in an.docs}
    pds["single runs pooled"] = pooled
    for name_, pd in pds.items():
        s, sw = an.score(pd), an.score(pd, weighted=True)
        m["luna_vs"][name_] = {"unweighted": s, "weighted": sw}
        rows.append([name_, int(s["tp"] + s["fn"]), fmt(s["precision"]), fmt(s["recall"]), fmt(s["f1"]),
                     fmt(sw["precision"]), fmt(sw["recall"]), fmt(sw["f1"])])
    md.append(f"**{pred_label} against references** (P / R / F1; w = design-weighted)\n")
    md += table(["reference", "ref units", "P", "R", "F1", "P w", "R w", "F1 w"], rows)

    # precision by support
    lun = [(d, k) for d in an.docs for k in an.luna_keys(d)]
    rows = []
    for s in (1, 2, 3, 4, 5, 6):
        n = sum(an.support[d][k] >= s for d, k in lun)
        nw = sum(an.w[d] * (an.support[d][k] >= s) for d, k in lun)
        rows.append([f">= {s}", n, fmt(n / len(lun) if lun else None),
                     fmt(nw / sum(an.w[d] for d, _ in lun) if lun else None)])
    zero = sum(an.support[d][k] == 0 for d, k in lun)
    rows.append(["0 (no strong run)", zero, fmt(zero / len(lun) if lun else None), ""])
    m["luna_units"] = len(lun)
    m["luna_support0"] = zero
    md.append(f"**{pred_label}'s units by strong-run support** ({len(lun)} units)\n")
    md += table(["support", "units", "share", "share w"], rows)

    # recall by attributes
    def recall_table(title: str, key: Callable[[Row], Any], order: Sequence[Any] | None = None,
                     bands: Sequence[tuple[str, Callable[[Row], bool]]] = (
                         ("support 1-2", lambda a: a["support"] <= 2), ("support 3", lambda a: a["support"] == 3),
                         ("support 4-6", lambda a: a["support"] >= 4))) -> None:
        groups: dict[Any, list[Row]] = defaultdict(list)
        for a in attrs.values():
            groups[key(a)].append(a)
        ks = list(order) if order else sorted(groups, key=lambda x: -len(groups[x]))
        out = []
        for g in ks:
            xs = groups.get(g, [])
            row = [g, len(xs), fmt(sum(a["luna"] for a in xs) / len(xs) if xs else None)]
            for _, f in bands:
                ys = [a for a in xs if f(a)]
                row.append(f"{fmt(sum(a['luna'] for a in ys) / len(ys) if ys else None, 2)} ({len(ys)})")
            out.append(row)
        md.append(f"**{pred_label} recall by {title}** (all candidate units; then by support band: recall (n))\n")
        md.extend(table([title, "units", "recall"] + [b for b, _ in bands], out))

    if an.unit == "mentions":
        pass
    elif an.unit in ("slugs", "nodes"):
        recall_table("modal confidence", lambda a: a["conf"], ["high", "med", "low"])
        recall_table("engagement", lambda a: engagement_band(a["engagement"]),
                     ["0 (mention/cite)", "1", "2", "3-4"])
        recall_table("primary role", lambda a: a["primary_role"])
    else:
        recall_table("modal confidence", lambda a: a["conf"], ["high", "med", "low"])
        recall_table("role", lambda a: a["role"])
        recall_table("median strength", lambda a: ("2" if a["median_strength"] < 3 else "3-4") if
                     a["median_strength"] >= 2 else "1", ["1", "2", "3-4"])

    # (d) leave one family out
    rows, m["lofo"] = [], {}
    lofo_pd: dict[tuple[str, str, int], dict[str, tuple[int, int, int]]] = {}
    for f in FAMILIES:
        others = [r for r in RUNS if family(r) != f]
        mine = [r for r in RUNS if family(r) == f]
        for k in (1, 2, 3, 4):
            ref = {d: an.ref(d, k, others) for d in an.docs}
            pd_f = {d: tuple(map(sum, zip(*[counts(an.runs[r][d], ref[d]) for r in mine]))) for d in an.docs}
            pd_l = {d: counts(an.luna_keys(d), ref[d]) for d in an.docs}
            lofo_pd[(f, "fam", k)], lofo_pd[(f, "luna", k)] = pd_f, pd_l
            sf, sl = an.score(pd_f), an.score(pd_l)
            m["lofo"][f"{f}|>={k}/4"] = {"family": sf, "luna": sl, "family_w": an.score(pd_f, weighted=True),
                                         "luna_w": an.score(pd_l, weighted=True)}
            rows.append([f, f">={k}/4", int(sl["tp"] + sl["fn"]),
                         f"{fmt(sf['precision'])} / {fmt(sf['recall'])} / {fmt(sf['f1'])}",
                         f"{fmt(sl['precision'])} / {fmt(sl['recall'])} / {fmt(sl['f1'])}"])
    md.append("**Leave one family out**: the other two families' four runs form the reference (>= k of 4); the "
              f"left-out family's two runs (pooled) and {pred_label} are scored against it (P / R / F1)\n")
    md += table(["left out", "reference", "ref units", "family runs", pred_label], rows)
    # recall by support among the other four runs
    rows = []
    for f in FAMILIES:
        others = [r for r in RUNS if family(r) != f]
        mine = [r for r in RUNS if family(r) == f]
        row = [f]
        for s in (1, 2, 3, 4):
            keys = [(d, k) for d in an.docs for k, n in Counter(x for r in others for x in an.runs[r][d]).items()
                    if n == s]
            fr = statistics.mean(sum(k in an.runs[r][d] for d, k in keys) / len(keys) for r in mine) if keys else None
            lr = sum(k in an.luna_keys(d) for d, k in keys) / len(keys) if keys else None
            row.append(f"{fmt(fr, 2)} / {fmt(lr, 2)} ({len(keys)})")
        rows.append(row)
    md.append(f"**Recall by support among the other four runs** (left-out family's run mean / {pred_label} (units))\n")
    md += table(["left out", "support 1/4", "2/4", "3/4", "4/4"], rows)

    # (f) breakdowns
    def strata_rows(pd: Mapping[str, tuple[int, int, int]]) -> list[list[str]]:
        out = []
        groups = [("all", an.docs)]
        groups += [(f"kind {k}", [d for d in an.docs if an.meta[d]["source_kind"] == k])
                   for k in ("paper", "forum", "blog", "card", "report")]
        groups += [(f"band {b}", [d for d in an.docs if an.meta[d]["band"] == b])
                   for b in ("<20k", "20-60k", "60-100k", ">100k")]
        groups += [(f"text {u}", [d for d in an.docs if an.meta[d]["usability"] == u]) for u in ("usable", "degraded")]
        groups += [("truncated (g3-0051, g3-0100)", [d for d in an.docs if d in TRUNCATED]),
                   ("all but truncated", [d for d in an.docs if d not in TRUNCATED])]
        for g, ds in groups:
            if not ds:
                continue
            s = an.score(pd, ds)
            out.append([g, len(ds), int(s["tp"] + s["fn"]), fmt(s["precision"]), fmt(s["recall"]), fmt(s["f1"])])
        return out
    md.append(f"**Breakdown: {pred_label} against >= 4/6** (P / R / F1)\n")
    md += table(["stratum", "docs", "ref units", "P", "R", "F1"], strata_rows(pds[">=4/6"]))
    md.append("**Breakdown: the strong runs against the leave-one-family-out >= 3/4 reference** (pooled over the six "
              f"runs) and {pred_label} against the same references (pooled over the three)\n")
    fam_pd = {d: tuple(map(sum, zip(*[lofo_pd[(f, "fam", 3)][d] for f in FAMILIES]))) for d in an.docs}
    lun_pd = {d: tuple(map(sum, zip(*[lofo_pd[(f, "luna", 3)][d] for f in FAMILIES]))) for d in an.docs}
    fr, lr = strata_rows(fam_pd), strata_rows(lun_pd)
    md += table(["stratum", "docs", "strong P / R / F1", f"{pred_label} P / R / F1"],
                [[a[0], a[1], f"{a[3]} / {a[4]} / {a[5]}", f"{b[3]} / {b[4]} / {b[5]}"] for a, b in zip(fr, lr)])
    for d in TRUNCATED:
        if d in an.docs:
            s = an.score(pds[">=4/6"], [d])
            beyond = extra_runs.get("beyond", {}).get(d)
            md.append(f"- {d}: >= 4/6 units {int(s['tp'] + s['fn'])}, {pred_label} found {int(s['tp'])}, fp "
                      f"{int(s['fp'])}" + (f"; >= 4/6 units whose every quote lies beyond 300k: {beyond}"
                                          if beyond is not None else ""))
    md.append("")

    # (e) bootstrap of the headline numbers
    if boot is not None:
        def stat(pd: Mapping[str, tuple[int, ...]], what: str, weighted: bool = False
                 ) -> Callable[[Sequence[str]], float | None]:
            return lambda ds: prf(*summed(pd, ds, an.w if weighted else None))[what]
        heads: list[tuple[str, Mapping[str, tuple[int, ...]], str]] = []
        for ref_ in (">=4/6", ">=3/6", ">=1/6", "single runs pooled"):
            for what in ("precision", "recall", "f1"):
                if ref_ == ">=1/6" and what == "f1":
                    continue
                heads.append((f"{pred_label} vs {ref_} {what}", pds[ref_], what))
        heads.append((f"{pred_label} recall on support 6/6", pds[">=6/6"], "recall"))
        lof_f = {d: tuple(map(sum, zip(*[lofo_pd[(f, "fam", 3)][d] for f in FAMILIES]))) for d in an.docs}
        lof_l = {d: tuple(map(sum, zip(*[lofo_pd[(f, "luna", 3)][d] for f in FAMILIES]))) for d in an.docs}
        for what in ("precision", "recall", "f1"):
            heads.append((f"strong runs vs LOFO >=3/4 {what}", lof_f, what))
            heads.append((f"{pred_label} vs LOFO >=3/4 {what}", lof_l, what))
        rows, m["bootstrap"] = [], {}
        for label, pd, what in heads:
            v, vw = prf(*summed(pd, an.docs))[what], prf(*summed(pd, an.docs, an.w))[what]
            lo, hi = boot.ci(stat(pd, what))
            lw, hw = boot.ci(stat(pd, what, True))
            m["bootstrap"][label] = {"value": v, "ci": [lo, hi], "weighted": vw, "ci_weighted": [lw, hw]}
            rows.append([label, ci_fmt(v, lo, hi), ci_fmt(vw, lw, hw)])
        for what in ("recall", "f1"):
            def diff(ds: Sequence[str], what: str = what, weighted: bool = False) -> float | None:
                w = an.w if weighted else None
                a, b = prf(*summed(lof_f, ds, w))[what], prf(*summed(lof_l, ds, w))[what]
                return None if a is None or b is None else a - b
            v, vw = diff(an.docs), diff(an.docs, weighted=True)
            lo, hi = boot.ci(diff)
            lw, hw = boot.ci(lambda ds: diff(ds, weighted=True))
            label = f"strong runs minus {pred_label}, LOFO >=3/4 {what}"
            m["bootstrap"][label] = {"value": v, "ci": [lo, hi], "weighted": vw, "ci_weighted": [lw, hw]}
            rows.append([label, ci_fmt(v, lo, hi), ci_fmt(vw, lw, hw)])
        md.append(f"**Headline with 95 % bootstrap intervals** ({len(boot.draws)} document resamples within source "
                  "kind)\n")
        md += table(["metric", "unweighted", "design-weighted"], rows)


def _mr_rows(label: Row, node: str) -> list[Row]:
    return [m for m in label.get("models") or [] if str(m.get("refined_node_id") or m["node_id"]) == node]


def qualitative(an: Analysis, md: list[str], metrics: Row, links: Callable[[str, str], bool] | None,
                groups: frozenset[str], luna_full: Mapping[str, Row], labels: Mapping[str, Mapping[str, Row]]
                ) -> None:
    """Most-missed solid units (support >= 5) and luna's unsupported units, by key component and by type."""
    name = f"{an.task}/{an.unit}"
    md.append(f"### Qualitative: {name}\n")
    miss: Counter = Counter()
    miss_type: Counter = Counter()
    fp0: Counter = Counter()
    fp_type: Counter = Counter()
    for d in an.docs:
        lk = an.luna_keys(d)
        for k, s in an.support[d].items():
            if s >= 5 and k not in lk:
                key = json.loads(k)
                miss[key if isinstance(key, str) else "|".join(key)] += 1
                if an.task == "techniques" and an.unit == "slugs":
                    lgrp = {str(t["slug"]) for t in luna_full[d].get("techniques") or []}
                    if not lgrp:
                        miss_type["luna has no technique row on the document"] += 1
                    elif links is not None and any(links(p, key) for p in lgrp):
                        miss_type["luna has a linked slug (parent / child / group)"] += 1
                    else:
                        miss_type["nothing linked in luna's rows"] += 1
                elif an.task == "model_roles" and an.unit == "cells2":
                    node, role = key
                    lr = {str(m_["node_id"]): m_.get("roles") or {} for m_ in luna_full[d].get("models") or []}
                    if node not in lr:
                        miss_type["node not in luna's rows (refinement / menu)"] += 1
                    elif int(lr[node].get(role) or 0) == 1:
                        miss_type["luna grades the cell 1"] += 1
                    elif not any(lr[node].values()):
                        miss_type["luna's row all zero"] += 1
                    else:
                        miss_type["luna grades the node, this role 0"] += 1
        for k in lk:
            if an.support[d][k] == 0:
                key = json.loads(k)
                fp0[key if isinstance(key, str) else "|".join(key)] += 1
                if an.task == "techniques" and an.unit == "slugs":
                    sup = {json.loads(x) for x in an.support[d]}
                    fp_type["a linked slug has strong support" if links is not None and
                            any(links(key, x) for x in sup) else "nothing linked has support"] += 1
                elif an.task == "model_roles":
                    node = key if isinstance(key, str) else key[0]
                    rows = [m for r in RUNS for m in _mr_rows(labels[r][d], node)]
                    if not rows:
                        fp_type["node in no strong run's rows (off-menu / refined away)"] += 1
                        continue
                    graded = [m for m in rows if any((m.get("roles") or {}).values())]
                    if an.unit != "nodes" and any(int((m.get("roles") or {}).get(key[1]) or 0) == 1 for m in rows):
                        fp_type["some strong run grades this cell 1 (luna over-grades)"] += 1
                    elif graded:
                        fp_type["strong runs grade the node, not this role at this strength"] += 1
                    else:
                        flags = Counter(str(m.get("flag") or "none").split(":")[0] for m in rows)
                        fp_type[f"node all-zero in every strong run (modal flag {flags.most_common(1)[0][0]})"] += 1
    total_solid = sum(1 for d in an.docs for s in an.support[d].values() if s >= 5)
    metrics.setdefault(name, {})["solid_missed"] = sum(miss.values())
    md.append(f"Solid units (support >= 5/6): {total_solid}; luna misses {sum(miss.values())}.\n")
    if miss_type:
        md += table(["miss type", "count"], miss_type.most_common())
    md += table(["most-missed solid unit", "docs"], miss.most_common(12))
    if an.unit != "slugs" and an.unit != "nodes":
        by_role: Counter = Counter()
        for k, v in miss.items():
            by_role[k.split("|")[1]] += v
        md += table(["missed solid cells by role", "count"], by_role.most_common())
    md += table(["luna unit with no strong support", "docs"], fp0.most_common(12))
    if fp_type:
        md += table(["type of luna unit with no strong support", "count"], fp_type.most_common())


# ── score ────────────────────────────────────────────────────────────────────────────────────


def beyond_window(root: Path, d: str, an: Analysis, labels: Mapping[str, Mapping[str, Row]]) -> int:
    """Units of >= 4/6 on document ``d`` whose every supporting row's quote first occurs beyond 300k characters."""
    text = (root / "docs" / d / "text.txt").read_text()
    n = 0
    for k, s in an.support[d].items():
        if s < 4:
            continue
        key = json.loads(k)
        slug = key if isinstance(key, str) else key[0]
        pos = []
        for r in RUNS:
            for t in labels[r][d].get("techniques") or labels[r][d].get("models") or []:
                if str(t.get("slug") or t.get("refined_node_id") or t.get("node_id")) == slug and t.get("quote"):
                    q = " ".join(str(t["quote"]).split())
                    i = " ".join(text.split()).find(q)
                    pos.append(i)
        if pos and all(p < 0 or p >= 300_000 for p in pos) and any(p >= 0 for p in pos):
            n += 1
    return n


def cmd_score(root: Path, resamples: int) -> None:
    sample = {r["item_id"]: r for r in read_jsonl(root / "sampling" / "sample.jsonl")}
    items = sorted(sample)
    kind, groups, link_pairs = taxonomy_structure(TAXONOMY)
    tech_slugs = [s for s in kind if s not in groups]

    class _Ctx:  # what tolerant_counts reads of a scoring context
        def __init__(self) -> None:
            self.groups = groups

        def linked(self, a: str, b: str) -> bool:
            return (a, b) in link_pairs or (b, a) in link_pairs

    ctx = _Ctx()
    out_dir = root / "analysis"
    out_dir.mkdir(exist_ok=True)
    md: list[str] = ["# Gold v3 analysis tables (generated by analysis.py score)\n"]
    metrics: Row = {"problems": {}, "luna_status": {}}
    mentions_by_doc = {r["doc_id"]: r for r in read_jsonl(root / "sampling" / "mentions.jsonl")}
    boot_cache: dict[tuple[str, ...], Boot] = {}
    for task in TASKS:
        labels, problems = load_labels(root, task, items)
        metrics["problems"][task] = problems
        luna_raw, status = load_luna(root, task, items)
        metrics["luna_status"][task] = dict(status)
        complete = [d for d in items if all(d in labels[r] for r in RUNS)]
        md.append(f"# {task}\n")
        md.append(f"Documents with all six runs: {len(complete)} of {len(items)}; luna rows {dict(status)}.\n")
        for p in problems:
            md.append(f"- {p}")
        md.append("")
        for unit in UNITS[task]:
            if task == "techniques":
                runs = {r: {d: tech_units(labels[r][d], unit, groups) for d in complete} for r in RUNS}
                luna = {d: tech_units(luna_raw[d], unit, groups) for d in complete}
                n_roles = 1 if unit == "slugs" else len(TECH_ROLES)
                universe = {d: len(tech_slugs) * n_roles for d in complete}
            else:
                runs = {r: {d: mr_units(labels[r][d], unit, False) for d in complete} for r in RUNS}
                if unit == "mentions":
                    luna = {d: {json.dumps(n): {"conf": None, "strength": 1, "roles": {}} for n in
                                mentions_by_doc.get(sample[d]["doc_id"], {}).get("strong", [])} for d in complete}
                    universe = None
                else:
                    luna = {d: mr_units(luna_raw[d], unit, True) for d in complete}
                    universe = {}
                    for d in complete:
                        nodes = {str(m["node_id"]) for r in RUNS for m in labels[r][d].get("models") or []}
                        nodes |= {str(m.get("matched_node_id") or m["node_id"]) for m in luna_raw[d].get("models") or []}
                        nodes |= {json.loads(k) if unit == "nodes" else json.loads(k)[0]
                                  for src in [*(runs[r][d] for r in RUNS), luna[d]] for k in src}
                        universe[d] = len(nodes) * (1 if unit == "nodes" else len(MR_ROLES))
            an = Analysis(task, unit, runs, luna, complete, universe, sample)
            key = tuple(complete)
            if key not in boot_cache:
                boot_cache[key] = Boot(complete, sample, resamples)
            extra: Row = {}
            if task == "techniques" and unit == "slugs":
                extra["beyond"] = {d: beyond_window(root, d, an, labels) for d in TRUNCATED if d in complete}
            analyse(an, boot_cache[key] if unit in ("slugs", "cells2", "nodes") else None, md, metrics, extra)
            if unit != "mentions":
                qualitative(an, md, metrics, ctx.linked if task == "techniques" else None, groups,
                            {d: luna_raw[d] for d in complete}, labels)
            if task == "techniques" and unit == "slugs":
                # hierarchy-tolerant luna vs each single run (the run as gold, its level alternatives honoured)
                rows = []
                for r in RUNS:
                    acc = [0.0, 0.0, 0.0, 0.0]
                    for d in complete:
                        g = {"label": {"techniques": labels[r][d].get("techniques") or []}}
                        ps = {str(t["slug"]) for t in luna_raw[d].get("techniques") or []}
                        acc = [a + b for a, b in zip(acc, tolerant_counts(g, "full", ps, ctx))]
                    p, rc = acc[2] / acc[3] if acc[3] else None, acc[0] / acc[1] if acc[1] else None
                    f1 = 2 * p * rc / (p + rc) if p and rc else None
                    strict = metrics[f"{task}/{unit}"]["luna_vs"][r]["unweighted"]
                    rows.append([r, f"{fmt(strict['precision'])} / {fmt(strict['recall'])} / {fmt(strict['f1'])}",
                                 f"{fmt(p)} / {fmt(rc)} / {fmt(f1)}"])
                md.append("**luna against each single run, strict vs hierarchy-tolerant** (`tolerant_counts`, the run "
                          "as gold)\n")
                md += table(["run", "strict P / R / F1", "tolerant P / R / F1"], rows)
            # normalised units
            with (out_dir / f"units-{task}-{unit}.jsonl").open("w") as fh:
                for d in complete:
                    for k in sorted(set(an.support[d]) | an.luna_keys(d)):
                        a = unit_attrs(an, d, k) if an.support[d][k] else {"support": 0, "runs": [], "luna": True}
                        fh.write(json.dumps({"item_id": d, "key": json.loads(k), **a,
                                             "source_kind": sample[d]["source_kind"], "band": sample[d]["band"],
                                             "usability": sample[d]["usability"], "weight": sample[d]["weight"]},
                                            sort_keys=True) + "\n")
        if problems:
            # sensitivity: the foreign files counted as their session's label, every document
            fl, _ = load_labels(root, task, items, accept_foreign=True)
            full = [d for d in items if all(d in fl[r] for r in RUNS)]
            md.append(f"**Sensitivity: foreign files counted as their session's label** ({len(full)} documents)\n")
            rows = []
            for unit in ("cells2", "nodes") if task == "model_roles" else ("slugs",):
                fn = (lambda lab, u=unit: mr_units(lab, u, False)) if task == "model_roles" else \
                    (lambda lab, u=unit: tech_units(lab, u, groups))
                ln = (lambda lab, u=unit: mr_units(lab, u, True)) if task == "model_roles" else \
                    (lambda lab, u=unit: tech_units(lab, u, groups))
                runs = {r: {d: fn(fl[r][d]) for d in full} for r in RUNS}
                an = Analysis(task, unit, runs, {d: ln(luna_raw[d]) for d in full}, full, None, sample)
                for k in (3, 4):
                    sc = an.score(an.per_doc(an.luna_keys, lambda d, k=k, an=an: an.ref(d, k)))
                    rows.append([unit, f">={k}/6", fmt(sc["precision"]), fmt(sc["recall"]), fmt(sc["f1"])])
            md += table(["unit", "reference", "P", "R", "F1"], rows)
        # extras / variants (counts only)
        rows = []
        if task == "model_roles":
            for r in RUNS:
                rows.append([r, sum(len(labels[r][d].get("extra_models") or []) for d in complete),
                             sum(len(labels[r][d].get("variant_models") or []) for d in complete),
                             sum(bool(m.get("refined_node_id")) for d in complete for m in labels[r][d]["models"])])
            rows.append(["luna", sum(len(luna_raw[d].get("extra_models") or []) for d in complete),
                         sum(len(luna_raw[d].get("variant_models") or []) for d in complete),
                         sum(str(m["node_id"]) != str(m.get("matched_node_id") or m["node_id"])
                             for d in complete for m in luna_raw[d].get("models") or [])])
            md.append("**Extras, variant rows and refinements** (counts, not scored)\n")
            md += table(["run", "extra_models", "variant_models", "refined rows"], rows)
    (out_dir / "tables.md").write_text("\n".join(md) + "\n")
    (out_dir / "metrics.json").write_text(json.dumps(metrics, indent=1, sort_keys=True) + "\n")
    print(f"wrote {out_dir}/tables.md, metrics.json, units-*.jsonl")


# ── blindness ────────────────────────────────────────────────────────────────────────────────


def cmd_blindness(root: Path, transcripts: Path, shared_tmp: str) -> None:
    sessions = root / "sessions"
    tops = sorted({Path(x).resolve().parts[1] for x in (root, transcripts, shared_tmp)})
    path_re = re.compile(r"(/(?:" + "|".join(map(re.escape, tops)) + r")/[^\s'\"`;|&)<>\\]+)")
    agents = []
    for f in sorted(transcripts.glob("wf_*/agent-*.jsonl")):
        sess, calls = None, []
        for line in f.open():
            e = json.loads(line)
            m = e.get("message") or {}
            c = m.get("content")
            if sess is None and isinstance(c, str):
                mm = re.search(r"gold-v3/sessions/([a-z_]+-[a-z]+-r[12])", c)
                sess = mm.group(1) if mm else None
            if isinstance(c, list) and m.get("role") == "assistant":
                calls += [(e.get("timestamp") or "", b["name"], b.get("input") or {}) for b in c
                          if b.get("type") == "tool_use"]
        if sess:
            agents.append((f.stem, sess, calls))
    print(f"labeller transcripts: {len(agents)}; per session {dict(Counter(s for _, s, _ in agents))}")
    cls: Counter = Counter()
    shared: dict[str, set[str]] = defaultdict(set)
    for ag, sess, calls in agents:
        own = str(sessions / sess)
        for _, _, inp in calls:
            for p in path_re.findall(json.dumps(inp)):
                if p.startswith(own):
                    k = "own session"
                elif p.startswith(str(sessions)):
                    k = "OTHER SESSION"
                elif p.startswith(str(root / "luna")):
                    k = "LUNA"
                elif p.startswith(str(root)):
                    k = "gold-v3 outside sessions"
                elif "/.claude/projects/" in p:
                    k = "own persisted tool output"
                else:
                    k = "temporary files"
                    shared[p.rstrip(".,")].add(ag)
                cls[k] += 1
    print("path references in tool calls:", dict(cls))
    dirs = {shared_tmp, *map(str, Path(shared_tmp).parents)}
    multi = {p: a for p, a in shared.items() if len(a) > 1 and p.rstrip("/") not in dirs}
    sess_of = {ag: s for ag, s, _ in agents}
    cross = {p: a for p, a in multi.items() if len({sess_of[x] for x in a}) > 1}
    print(f"temporary paths used by > 1 agent: {len(multi)}; by agents of > 1 session: {len(cross)}")
    for p, a in sorted(cross.items()):
        print(f"  {p.replace(shared_tmp, '$TMP')}: {sorted(Counter(sess_of[x] for x in a).items())}")
    # content comparison
    for task in TASKS:
        print(f"== {task}: identical content across runs")
        items = sorted(f.stem for f in (sessions / f"{task}-{RUNS[0]}" / "out").glob("g3-*.json"))
        for a, b in itertools.combinations(RUNS, 2):
            whole, rat, rows = [], 0, 0
            for iid in items:
                x = json.loads((sessions / f"{task}-{a}" / "out" / f"{iid}.json").read_text())
                y = json.loads((sessions / f"{task}-{b}" / "out" / f"{iid}.json").read_text())
                if {k: v for k, v in x.items() if k != "labeller"} == {k: v for k, v in y.items() if k != "labeller"}:
                    whole.append(iid)
                note = "doc_note" if task == "techniques" else "rationale"
                rat += bool(x.get(note)) and x.get(note) == y.get(note)
                if task == "techniques":
                    ka = {(t["slug"], t["quote"], t["reason"]) for t in x["techniques"]}
                    kb = {(t["slug"], t["quote"], t["reason"]) for t in y["techniques"]}
                else:
                    ka = {(m["node_id"], m["quote"], m["note"]) for m in x["models"] if m["quote"] and m["note"]}
                    kb = {(m["node_id"], m["quote"], m["note"]) for m in y["models"] if m["quote"] and m["note"]}
                rows += len(ka & kb)
            if whole or rat or rows:
                print(f"  {a} vs {b}: identical files {whole}, identical doc note/rationale {rat}, "
                      f"identical rows (quote + reason/note) {rows}")


# ── arms (luna variants against the same references) ────────────────────────────────────────


def _union_row(rows: Sequence[Row]) -> Row:
    """Several eval rows of one document as one prediction: units unioned (:func:`_row_units`), costs and tokens
    summed, latency the slowest (the calls run side by side), ``ok`` when any is."""
    return {"_union": list(rows), "status": "ok" if any(r.get("status") == "ok" for r in rows) else "failed",
            "failed_parts": sum(r.get("status") != "ok" for r in rows),
            "tokens_out": sum(int(r.get("tokens_out") or 0) for r in rows),
            "schema_degraded": any(r.get("schema_degraded") for r in rows),
            "standalone_latency_ms": max(int(_latency(r) or 0) for r in rows)}


def _read_run(run_dir: str, facts: Row) -> dict[int, dict[str, Row]]:
    by_rep: dict[int, dict[str, Row]] = defaultdict(dict)
    run = json.loads((Path(run_dir) / "run.json").read_text())
    facts["runs"].append(Path(run_dir).name)
    facts["settled_usd"] += float(run.get("settled_usd") or 0.0)
    for r in read_jsonl(Path(run_dir) / "rows.jsonl"):
        by_rep[int(r.get("repeat") or 1)][r["item_id"]] = r
    return by_rep


def _read_term(term: str, facts: Row) -> dict[int, dict[str, Row]]:
    """``RUN`` (its repeats), ``RUN%`` (one replicate: the union of its repeats) or ``A+B[+...]`` (replicate k = the
    union of each run's repeat k)."""
    if term.endswith("%"):
        by = _read_run(term[:-1], facts)
        ids = set().union(*(v.keys() for v in by.values()))
        return {1: {d: _union_row([by[k][d] for k in sorted(by) if d in by[k]]) for d in ids}}
    if "+" in term:
        runs = [_read_run(t, facts) for t in term.split("+")]
        ks = sorted(set.intersection(*(set(r) for r in runs)))
        return {k: {d: _union_row([r[k][d] for r in runs]) for d in set.intersection(*(set(r[k]) for r in runs))}
                for k in ks}
    return _read_run(term, facts)


def load_run_rows(spec: str, items: Sequence[str], chars: Mapping[str, int] | None = None
                  ) -> tuple[list[dict[str, Row]], Row]:
    """Replicates (``item -> eval row``) and run facts of one ``--arm`` value (module docstring, ``arms``)."""
    reps: list[dict[str, Row]] = []
    facts: Row = {"runs": [], "settled_usd": 0.0}
    for part in spec.split(","):
        base, _, overlay = part.partition("@")
        overlay, _, above = overlay.partition(">")
        by_rep = _read_term(base, facts)
        if overlay:
            over = _read_term(overlay, facts)
            for k in by_rep:
                src = over.get(k) or over[max(over)]  # a single-replicate overlay covers every repeat
                for d, r in src.items():
                    if not above or (chars or {}).get(d, 0) > int(above):
                        by_rep[k][d] = r  # the overlay wins (all its items, or those longer than ``above``)
        for k in sorted(by_rep):
            missing = [d for d in items if d not in by_rep[k]]
            if missing:
                raise SystemExit(f"arm {spec}: repeat {k} lacks {len(missing)} items ({missing[:3]} ...)")
            reps.append(by_rep[k])
    return reps, facts


def _row_units(task: str, unit: str, row: Row, groups: frozenset[str]) -> Units:
    if "_union" in row:
        out: Units = {}
        for r in row["_union"]:
            out.update(_row_units(task, unit, r, groups))
        return out
    payload = (row.get("payload") or {}) if row.get("status") == "ok" else {}
    return tech_units(payload, unit, groups) if task == "techniques" else mr_units(payload, unit, True)


#: gpt-6-luna prices, USD per token (OpenRouter's listing, 2026-10-23): uncached input is billed as a cache write
#: (1.25e-7; every uncached token of the eval calls is reported as written), cache reads 1e-8, output 5e-7. Checked
#: on the baseline's first row: 14,432 x 1.25e-7 + 39,447 x 1e-8 + 5,210 x 5e-7 = the reported $0.004803.
LUNA_IN, LUNA_OUT, LUNA_CACHED = 0.125e-6, 0.5e-6, 0.01e-6


def system_tokens(reps: Sequence[Mapping[str, Row]]) -> int:
    """The arm's cached system prefix in tokens: the smallest ``cached_tokens`` over 1,000 of a document's first
    call (a hit always covers the whole system prefix; a larger count also covers document tokens another arm or
    run sent before, which a fresh run would not find cached)."""
    flat = [x for rep in reps for r in rep.values() for x in r.get("_union", [r])]
    c = [int((r.get("calls") or [r])[0].get("cached_tokens") or 0) for r in flat]
    c = [x for x in c if x > 1000]
    return min(c) if c else 0


def fresh_cost(row: Row, sys_tokens: int) -> float:
    """A token-based first-encounter cost of one row: every call's system prefix cached, the first call's
    document uncached at list price (no reuse of another arm's or run's provider cache), later calls of the
    same document (a gleaning turn, window parts) with the cached tokens they reported (capped below by the
    system prefix); output at list (prices: :data:`LUNA_IN`). Ignores the listing's long-context tier (> 272k prompt tokens)."""
    if "_union" in row:
        return sum(fresh_cost(r, sys_tokens) for r in row["_union"])
    calls = row.get("calls") or ([row] if row.get("prompt_tokens") is not None else [])
    usd = 0.0
    for k, c in enumerate(calls):
        prompt = int(c.get("prompt_tokens") or c.get("tokens_in") or 0)
        cached = min(prompt, sys_tokens) if k == 0 or c.get("stage") == "window" else \
            max(min(prompt, sys_tokens), int(c.get("cached_tokens") or 0))
        usd += (prompt - cached) * LUNA_IN + cached * LUNA_CACHED + int(c.get("tokens_out") or 0) * LUNA_OUT
    return usd


def _latency(row: Row) -> float | None:
    """The document's wall time in ms as a fresh run would see it (``standalone_latency_ms``: a call the eval's
    response cache served counts at its original latency); None when unknown (a cache hit without it, e.g. a
    model_roles row)."""
    if row.get("standalone_latency_ms") is not None:
        return float(row["standalone_latency_ms"])
    if row.get("cache_hit") or row.get("latency_ms") is None:
        return None
    return float(row["latency_ms"])


def _row_cost(row: Row) -> float:
    if "_union" in row:
        return sum(_row_cost(r) for r in row["_union"])
    for k in ("standalone_cost_usd", "call_cost_usd", "cost_usd"):
        if row.get(k) is not None:
            return float(row[k])
    return 0.0


def _failed(row: Row) -> int:
    return int(row["failed_parts"]) if "_union" in row else int(row.get("status") != "ok")


def cmd_arms(root: Path, task: str, arm_specs: Sequence[str], baseline: str, resamples: int,
             split_file: Path | None = None, exclude: Sequence[str] = (), tag: str = "") -> None:
    sample = {r["item_id"]: r for r in read_jsonl(root / "sampling" / "sample.jsonl")}
    items = sorted(sample)
    _kind, groups, _links = taxonomy_structure(TAXONOMY)
    labels, problems = load_labels(root, task, items)
    docs = [d for d in items if all(d in labels[r] for r in RUNS) and d not in set(exclude)]
    arms: dict[str, tuple[list[dict[str, Row]], Row]] = {}
    for spec in arm_specs:
        label, _, runs = spec.partition("=")
        arms[label] = load_run_rows(runs, items, {d: int(sample[d]["chars"]) for d in items})
    if baseline not in arms:
        raise SystemExit(f"baseline {baseline!r} is not an --arm label ({sorted(arms)})")
    boot = Boot(docs, sample, resamples)
    units = ("slugs", "cells2") if task == "techniques" else ("cells2", "nodes")
    dense = ("g3-0097", "g3-0018", "g3-0100")
    out: Row = {"task": task, "documents": len(docs), "problems": problems, "baseline": baseline, "arms": {}}
    md = [f"# Arms on gold v3, {task} ({len(docs)} documents with all six strong runs; generated by analysis.py arms)\n",
          f"Baseline `{baseline}`. P / R / F1 micro, replicates pooled (counts summed); bootstrap {resamples} document "
          "resamples within source kind, 95 % percentile intervals; paired differences use the same resamples.\n"]
    for p in problems:
        md.append(f"- {p}")
    per_doc: dict[tuple[str, str, int], dict[str, tuple[int, int, int]]] = {}
    for unit in units:
        runs_u = {r: {d: (tech_units(labels[r][d], unit, groups) if task == "techniques"
                          else mr_units(labels[r][d], unit, False)) for d in docs} for r in RUNS}
        an = Analysis(task, unit, runs_u, None, docs, None, sample)
        for label, (reps, _f) in arms.items():
            preds = [{d: _row_units(task, unit, rep[d], groups) for d in docs} for rep in reps]
            for k in (1, 4, 6):
                ref = {d: an.ref(d, k) for d in docs}
                pooled = {d: tuple(map(sum, zip(*[counts(p[d], ref[d]) for p in preds]))) for d in docs}
                per_doc[(label, unit, k)] = pooled
            zero = sum(1 for p in preds for d in docs for key in p[d] if not an.support[d][key])
            out["arms"].setdefault(label, {}).setdefault("unsupported_per_replicate", {})[unit] = zero / len(preds)
            if unit == units[0]:
                by_support = {}
                for sup in range(1, 7):
                    keys = [(d, key) for d in docs for key, n in an.support[d].items() if n == sup]
                    by_support[sup] = (sum(key in p[d] for p in preds for d, key in keys) / (len(keys) * len(preds))
                                       if keys else None)
                out["arms"].setdefault(label, {})["recall_by_support"] = by_support
                ref4 = {d: an.ref(d, 4) for d in docs}
                out["arms"][label]["dense_recall"] = {
                    d: (sum(len(set(p[d]) & ref4[d]) for p in preds) / (len(ref4[d]) * len(preds)) if ref4[d] else None)
                    for d in dense if d in docs}
    def stat(label: str, unit: str, k: int, metric: str) -> Callable[[Sequence[str]], float | None]:
        return lambda ds: prf(*summed(per_doc[(label, unit, k)], ds))[metric]
    rows_main, rows_ci, rows_extra = [], [], []
    for label, (reps, facts) in arms.items():
        a = out["arms"][label]
        a["replicates"] = len(reps)
        a["runs"] = facts["runs"]
        a["settled_usd"] = facts["settled_usd"]
        n_rows = sum(len(rep) for rep in reps)
        a["usd_per_1k_standalone"] = 1000 * sum(_row_cost(r) for rep in reps for r in rep.values()) / n_rows
        st = system_tokens(reps)
        a["system_tokens"] = st
        a["usd_per_1k_fresh"] = 1000 * sum(fresh_cost(r, st) for rep in reps for r in rep.values()) / n_rows
        a["failures"] = sum(_failed(r) for rep in reps for r in rep.values())
        a["schema_degraded"] = sum(bool(r.get("schema_degraded")) for rep in reps for r in rep.values())
        a["empty"] = sum(r.get("status") == "ok" and r.get("ann_status") == "empty" for rep in reps for r in rep.values())
        a["tokens_out_mean"] = sum(int(r.get("tokens_out") or 0) for rep in reps for r in rep.values()) / n_rows
        for unit in units:
            for k in (1, 4, 6):
                a[f"{unit}>={k}"] = prf(*summed(per_doc[(label, unit, k)], docs))
        u = units[0]
        a["ci"] = {f"{u}>=4:{m}": boot.ci(stat(label, u, 4, m)) for m in ("precision", "recall", "f1")}
        if label != baseline:
            a["diff_vs_baseline"] = {}
            for m in ("precision", "recall", "f1"):
                pt = a[f"{u}>=4"][m] - prf(*summed(per_doc[(baseline, u, 4)], docs))[m]
                sa, sb = stat(label, u, 4, m), stat(baseline, u, 4, m)
                lo, hi = boot.ci(lambda ds, sa=sa, sb=sb: (sa(ds) or 0.0) - (sb(ds) or 0.0))
                a["diff_vs_baseline"][m] = (pt, lo, hi)
        s4, s1, s6 = a[f"{u}>=4"], a[f"{u}>=1"], a[f"{u}>=6"]
        v2 = a[f"{units[1]}>=4"]
        rows_main.append([label, a["replicates"], f"{fmt(s4['precision'])} / {fmt(s4['recall'])} / {fmt(s4['f1'])}",
                          fmt(s6["recall"]), fmt(s1["precision"]),
                          f"{fmt(v2['precision'])} / {fmt(v2['recall'])} / {fmt(v2['f1'])}",
                          f"{a['usd_per_1k_fresh']:.2f} ({a['usd_per_1k_standalone']:.2f})", a["failures"],
                          a["schema_degraded"],
                          f"{a['tokens_out_mean']:.0f}"])
        ci = a["ci"]
        d = a.get("diff_vs_baseline") or {}
        rows_ci.append([label] + [ci_fmt(a[f"{u}>=4"][m], *ci[f"{u}>=4:{m}"]) for m in ("precision", "recall", "f1")] +
                       [ci_fmt(*d[m]) if m in d else "-" for m in ("precision", "recall", "f1")])
        rows_extra.append([label] + [fmt(a["recall_by_support"][k], 2) for k in range(1, 7)] +
                          [fmt(v, 2) for v in a["dense_recall"].values()] +
                          [f"{a['unsupported_per_replicate'][x]:.1f}" for x in units])
    second = "cells >= 2" if task == "techniques" else "nodes"
    md += [f"## {u} (primary)\n"]
    md += table(["arm", "reps", f"{u} >= 4/6 P / R / F1", "R on 6/6", "P vs union", f"{second} >= 4/6 P / R / F1",
                 "$/1k fresh (standalone)", "failed", "schema degraded", "mean out tokens"], rows_main)
    md += table(["arm", "P [CI]", "R [CI]", "F1 [CI]", "dP vs base [CI]", "dR vs base [CI]", "dF1 vs base [CI]"], rows_ci)
    md += table(["arm"] + [f"R sup {k}" for k in range(1, 7)] + [f"R {d}" for d in dense] +
                [f"unsupported {x} / rep" for x in units],
                rows_extra)
    if split_file is not None:
        md += split_tables(task, units[0], arms, baseline, per_doc, docs, sample, json.loads(split_file.read_text()),
                           resamples, out)
    (root / "analysis").mkdir(exist_ok=True)
    name = f"arms-{task}{'-' + tag if tag else ''}"
    (root / "analysis" / f"{name}.md").write_text("\n".join(md) + "\n")
    (root / "analysis" / f"{name}.json").write_text(json.dumps(out, indent=1, sort_keys=True, default=str))
    print("\n".join(md))


def split_tables(task: str, u: str, arms: Mapping[str, tuple[list[dict[str, Row]], Row]], baseline: str,
                 per_doc: Mapping[tuple[str, str, int], Mapping[str, tuple[int, int, int]]], docs: Sequence[str],
                 sample: Mapping[str, Row], split: Row, resamples: int, out: Row) -> list[str]:
    """Per split (dev, test, all): each arm's P / R / F1 against >= 4/6, recall on 6/6, precision against the union,
    the paired F1 difference to the baseline with its bootstrap interval, token-based fresh cost per 1,000
    documents, median document latency and failures (``--split-file``, ``scripts/split.py``)."""
    md = [f"## By split ({u}; split file seed {split.get('seed')})\n"]
    out["splits"] = {}
    for name, ds in (("dev", [d for d in docs if d in set(split["dev"])]),
                     ("test", [d for d in docs if d in set(split["test"])]), ("all", list(docs))):
        boot = Boot(ds, sample, resamples)
        rows = []
        for label, (reps, _f) in arms.items():
            st = system_tokens(reps)
            rr = [rep[d] for rep in reps for d in ds]
            m4, m6, m1 = (prf(*summed(per_doc[(label, u, k)], ds)) for k in (4, 6, 1))
            lat = sorted(x for x in (_latency(r) for r in rr) if x is not None)
            cell: Row = {"p": m4["precision"], "r": m4["recall"], "f1": m4["f1"], "r6": m6["recall"],
                         "p_union": m1["precision"], "usd_per_1k_fresh": 1000 * sum(fresh_cost(r, st) for r in rr) / len(rr),
                         "latency_p50_s": lat[len(lat) // 2] / 1000 if lat else None,
                         "failures": sum(_failed(r) for r in rr), "replicates": len(reps)}
            d = "-"
            if label != baseline:
                sa = lambda x, label=label: prf(*summed(per_doc[(label, u, 4)], x))["f1"]
                sb = lambda x: prf(*summed(per_doc[(baseline, u, 4)], x))["f1"]
                pt = (sa(ds) or 0.0) - (sb(ds) or 0.0)
                lo, hi = boot.ci(lambda x, sa=sa, sb=sb: (sa(x) or 0.0) - (sb(x) or 0.0))
                cell["df1"] = (pt, lo, hi)
                d = ci_fmt(pt, lo, hi)
            out["splits"].setdefault(name, {})[label] = cell
            rows.append([label, cell["replicates"], f"{fmt(cell['p'])} / {fmt(cell['r'])} / {fmt(cell['f1'])}", d,
                         fmt(cell["r6"]), fmt(cell["p_union"]), f"{cell['usd_per_1k_fresh']:.2f}",
                         fmt(cell["latency_p50_s"], 1), cell["failures"]])
        md.append(f"### {name} ({len(ds)} documents)\n")
        md += table(["arm", "reps", "P / R / F1 vs >= 4/6", f"dF1 vs {baseline} [CI]", "R 6/6", "P vs union",
                     "$/1k fresh", "p50 s/doc", "failed"], rows)
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("score")
    s.add_argument("gold_dir", type=Path)
    s.add_argument("--resamples", type=int, default=2000)
    b = sub.add_parser("blindness")
    b.add_argument("gold_dir", type=Path)
    b.add_argument("transcripts", type=Path)
    b.add_argument("shared_tmp")
    c = sub.add_parser("arms")
    c.add_argument("gold_dir", type=Path)
    c.add_argument("task", choices=TASKS)
    c.add_argument("--arm", action="append", required=True, help="LABEL=RUN_DIR[@OVERLAY][,RUN_DIR...]")
    c.add_argument("--baseline", required=True)
    c.add_argument("--resamples", type=int, default=2000)
    c.add_argument("--split-file", type=Path, default=None, help="split.json (scripts/split.py): dev / test / all")
    c.add_argument("--tag", default="", help="output name suffix: analysis/arms-<task>-<tag>.{md,json}")
    c.add_argument("--exclude", default="", help="comma-separated item ids left out of every table (sensitivity)")
    a = ap.parse_args()
    if a.cmd == "score":
        cmd_score(a.gold_dir, a.resamples)
    elif a.cmd == "arms":
        cmd_arms(a.gold_dir, a.task, a.arm, a.baseline, a.resamples, a.split_file,
                 [x for x in (a.exclude or "").split(",") if x], a.tag)
    else:
        cmd_blindness(a.gold_dir, a.transcripts, a.shared_tmp.rstrip("/"))


if __name__ == "__main__":
    main()
