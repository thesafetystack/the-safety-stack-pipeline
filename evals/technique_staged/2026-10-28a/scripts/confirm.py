"""The effort-high confirmation on the held-out test set (``PROTOCOL.md``): units, adjudication packets, the adjudicated
reference ref-t and the scores. No LLM call; the adjudicators and arbiters run outside this script.

Run from the repo root (paths resolve from this file)::

    PY="uv run --no-sync python evals/technique_staged/2026-10-28a/scripts/confirm.py"
    $PY units     --private P --run HALL=<run dir> --run BASEF=<run dir>
    $PY packets   --private P --tasks <judge task dirs>/technique_staged
    $PY arb-packets --private P                 # after both adjudicators answered every batch
    $PY reference --private P                   # after both arbiters answered every arbitration batch
    $PY score     --private P --run HALL=<run dir> --run BASEF=<run dir>

``P`` is a private working directory (it holds document text and quotes: never in the repository). Inputs read from
the repository: ``protocol.json``, the judge set's ``production.jsonl`` and ``labels-{opus,sonnet,sol}.jsonl``,
taxonomy 1.0.0 and, for the packets, 114's adjudication guide, arbitration guide and checker
(``../2026-10-23a/adjudication/``), rubric ``rubric-v0.9-c`` and the judges' catalogue. Outputs in the repository
(slug level, no text): ``results/reference-t.jsonl``, ``results/scores.json``, ``results/scores.md``.

Answers: Claude adjudicators write ``P/adj/sessions/<label>/<batch>/out/<item>.json``; codex answers are
``P/adj/runs/<label>/<batch>.answer.json`` (``{"documents": [...]}``); arbitration the same under ``P/adj/arb/``.
Labels (fixed here): adjudicators ``opus-t`` and ``sol-t``, arbiters ``sol-arb`` and ``fable-arb``.
"""
from __future__ import annotations

import argparse
import json
import math
import random
import re
import shutil
import statistics
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]  # the repository root
HERE = ROOT / "evals/technique_staged/2026-10-28a"
JSET = ROOT / "evals/judge-2026-10-25j"
ADJ_SRC = ROOT / "evals/technique_staged/2026-10-23a/adjudication"
RUBRIC = ROOT / "workflows/annotate/technique_staged/prompts/rubric-v0.9-c.md"
CATALOGUE = JSET / "guides/technique_staged-catalogue.md"
TAXONOMY = ROOT / "taxonomy/techniques-1.0.0.yaml"
JUDGES = ("opus", "sonnet", "sol")
JUDGE_OK = ("valid", "repaired", "over_length")
ADJUDICATORS = ("opus-t", "sol-t")
ARBITERS = ("sol-arb", "fable-arb")
SEED = 20261028
BANDS = ((20_000, "<20k"), (60_000, "20-60k"), (100_000, "60-100k"))
Row = dict[str, Any]
Ref = dict[str, dict[str, str]]  # item -> slug -> in | dont_care (out units absent)


def read_jsonl(p: Path) -> list[Row]:
    return [json.loads(x) for x in p.read_text("utf-8").splitlines() if x.strip()]


def write_jsonl(p: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(r, sort_keys=True, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def protocol() -> Row:
    return json.loads((HERE / "protocol.json").read_text("utf-8"))


def taxonomy() -> tuple[frozenset[str], frozenset[str], dict[str, Row]]:
    """(valid technique slugs incl. group nodes, group slugs, entries by slug)."""
    import yaml

    raw = yaml.safe_load(TAXONOMY.read_text("utf-8"))
    ent = {t["slug"]: t for t in raw["techniques"]}
    groups = frozenset(g["slug"] for g in raw["group_nodes"])
    ent.update({g["slug"]: {**g, "kind": "group"} for g in raw["group_nodes"]})
    return frozenset(ent), groups, ent


# ── sources: judges, production, eval runs ─────────────────────────────────────────────────────


def label_units(label: Mapping[str, Any] | None, valid: frozenset[str], groups: frozenset[str],
                invalid: Counter | None = None, who: str = "") -> dict[str, list[str]]:
    """slug -> quotes, for every slug of the taxonomy with a role of strength >= 1, group nodes out."""
    out: dict[str, list[str]] = {}
    for t in (label or {}).get("techniques") or []:
        s = t.get("slug")
        if s not in valid:
            if invalid is not None:
                invalid[who] += 1
            continue
        if s in groups:
            continue
        if not any(isinstance(v, int) and v > 0 for v in (t.get("roles") or {}).values()):
            continue
        qs = out.setdefault(s, [])
        for q in t.get("quotes") or []:
            q = (q.get("text") if isinstance(q, Mapping) else q) or ""
            if q.strip() and q.strip() not in qs:
                qs.append(q.strip())
    return out


def parse_runs(specs: Sequence[str]) -> dict[str, Path]:
    out = {}
    for s in specs:
        name, _, path = s.partition("=")
        if not path:
            raise SystemExit(f"--run {s!r}: want NAME=<run dir>")
        out[name] = Path(path)
    return out


def run_rows(run_dir: Path) -> list[dict[str, Row]]:
    """Replicates [item -> eval row] of one run (rows.jsonl, ``repeat`` 1..k)."""
    by: dict[int, dict[str, Row]] = defaultdict(dict)
    for r in read_jsonl(run_dir / "rows.jsonl"):
        by[int(r.get("repeat") or 1)][r["item_id"]] = r
    return [by[k] for k in sorted(by)]


class Sources:
    """Every source's units per item: judges (one observation each) and arms (observations = replicates)."""

    def __init__(self, runs: Mapping[str, Path]) -> None:
        self.valid, self.groups, self.ent = taxonomy()
        self.items = [i["item_id"] for i in protocol()["items"]]
        self.meta = {i["item_id"]: i for i in protocol()["items"]}
        self.invalid: Counter = Counter()
        self.judge_status: dict[str, Counter] = {}
        self.judges: dict[str, dict[str, dict[str, list[str]] | None]] = {}
        for j in JUDGES:
            rows = {r["item_id"]: r for r in read_jsonl(JSET / "technique_staged" / f"labels-{j}.jsonl")}
            self.judge_status[j] = Counter(rows[i]["status"] if i in rows else "missing" for i in self.items)
            self.judges[j] = {i: (label_units(rows[i]["label"], self.valid, self.groups, self.invalid, j)
                                  if i in rows and rows[i]["status"] in JUDGE_OK else None) for i in self.items}
        prod = {r["item_id"]: r for r in read_jsonl(JSET / "technique_staged" / "production.jsonl")}
        self.arms: dict[str, list[dict[str, dict[str, list[str]]]]] = {
            "PROD": [{i: label_units(prod[i]["label"] if prod[i]["status"] in ("ok", "empty") else None,
                                     self.valid, self.groups, self.invalid, "PROD") for i in self.items}]}
        self.rows: dict[str, list[dict[str, Row]]] = {}
        for name, d in runs.items():
            reps = run_rows(d)
            self.rows[name] = reps
            self.arms[name] = [{i: label_units(rep[i].get("payload") if rep.get(i, {}).get("status") == "ok" else None,
                                               self.valid, self.groups, self.invalid, name)
                                for i in self.items} for rep in reps]

    def slugs(self, arm: str) -> list[dict[str, set[str]]]:
        return [{i: set(rep[i]) for i in self.items} for rep in self.arms[arm]]

    def judge_slugs(self, j: str) -> dict[str, set[str]]:
        return {i: set(self.judges[j][i] or {}) for i in self.items}

    def units(self) -> list[Row]:
        out = []
        for i in self.items:
            slugs: set[str] = set()
            for j in JUDGES:
                slugs |= set(self.judges[j][i] or {})
            for reps in self.arms.values():
                for rep in reps:
                    slugs |= set(rep[i])
            for s in sorted(slugs):
                quotes: list[str] = []
                judges = [j for j in JUDGES if s in (self.judges[j][i] or {})]
                for j in judges:
                    quotes += [q for q in self.judges[j][i][s] if q not in quotes]
                arms = {}
                for a, reps in self.arms.items():
                    arms[a] = [int(s in rep[i]) for rep in reps]
                    for rep in reps:
                        quotes += [q for q in rep[i].get(s, []) if q not in quotes]
                out.append({"item_id": i, "slug": s, "support": len(judges), "judges": judges, "arms": arms,
                            "quotes": quotes})
        return out


# ── packets ────────────────────────────────────────────────────────────────────────────────────


def entry_view(e: Mapping[str, Any]) -> Row:
    al = e.get("aliases") or {}
    return {"slug": e["slug"], "name": e["name"], "kind": e.get("kind"), "definition": e.get("definition"),
            "aliases": al.get("synonym", []), "named_instances": al.get("instance", []), "parent": e.get("parent")}


def batches_of(docs: Sequence[str], chars: Mapping[str, int], max_docs: int, max_chars: int) -> list[list[str]]:
    out: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for d in sorted(docs, key=lambda d: (chars[d], d)):
        if cur and (len(cur) >= max_docs or n + chars[d] > max_chars):
            out.append(cur)
            cur, n = [], 0
        cur.append(d)
        n += chars[d]
    if cur:
        out.append(cur)
    return out


def cmd_packets(args: argparse.Namespace) -> None:
    P = Path(args.private)
    units = read_jsonl(P / "units.jsonl")
    _v, _g, ent = taxonomy()
    meta = {i["item_id"]: i for i in protocol()["items"]}
    rng = random.Random(SEED)
    by_doc: dict[str, list[Row]] = defaultdict(list)
    for u in units:
        by_doc[u["item_id"]].append(u)
    key, cands = [], {}
    for d in sorted(by_doc):
        us = by_doc[d]
        chosen = [u for u in us if u["support"] < 3]
        if not chosen:
            continue
        solid = [u for u in us if u["support"] == 3]
        calib = [rng.choice(solid)] if solid else []
        chosen = chosen + calib
        rng.shuffle(chosen)
        cs = []
        for u in chosen:
            qs = list(u["quotes"])
            rng.shuffle(qs)
            cs.append({**entry_view(ent[u["slug"]]), "pointer_quotes": qs[:4]})
            key.append({"item_id": d, "slug": u["slug"], "support": u["support"], "calibration": u in calib})
        cands[d] = cs
    out = P / "adj"
    if (out / "batches").exists():
        raise SystemExit(f"{out / 'batches'} exists; move it aside first")
    (out / "batches").mkdir(parents=True)
    write_jsonl(out / "key.jsonl", key)
    tasks = Path(args.tasks)
    plan = []
    for k, ds in enumerate(batches_of(list(cands), {d: meta[d]["chars"] for d in cands}, 4, 250_000), 1):
        b = out / "batches" / f"B{k:02d}"
        (b / "out").mkdir(parents=True)
        (b / "work").mkdir()
        for src, name in ((ADJ_SRC / "GUIDE.md", "GUIDE.md"), (RUBRIC, "rubric.md"), (CATALOGUE, "catalogue.md"),
                          (ADJ_SRC / "check.py", "check.py")):
            shutil.copyfile(src, b / name)
        for d in ds:
            dd = b / "docs" / d
            dd.mkdir(parents=True)
            for f in ("text.txt", "meta.md"):
                shutil.copyfile(tasks / d / f, dd / f)
            (dd / "candidates.json").write_text(json.dumps({"item_id": d, "candidates": cands[d]}, indent=1,
                                                           ensure_ascii=False), "utf-8")
        (b / "batch.json").write_text(json.dumps({"batch": b.name, "items": ds}, indent=1), "utf-8")
        plan.append({"batch": b.name, "items": ds, "candidates": sum(len(cands[d]) for d in ds),
                     "chars": sum(meta[d]["chars"] for d in ds)})
    (out / "plan.json").write_text(json.dumps(plan, indent=1), "utf-8")
    sup = Counter(k["support"] for k in key if not k["calibration"])
    print(f"{len(cands)} docs, {len(key)} candidates (support 0/1/2: {sup[0]}/{sup[1]}/{sup[2]}, "
          f"calibration {sum(k['calibration'] for k in key)}), {len(plan)} batches")


def load_answers(base: Path, label: str) -> dict[tuple[str, str], Row]:
    """(item, slug) -> decision of one adjudicator / arbiter (codex answer file, else the Claude session's out/)."""
    out: dict[tuple[str, str], Row] = {}
    for b in sorted((base / "batches").iterdir()):
        docs: list[Row] = []
        a = base / "runs" / label / f"{b.name}.answer.json"
        if a.exists() and a.stat().st_size:
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", a.read_text("utf-8").strip())
            try:
                docs = json.loads(raw)["documents"]
            except (json.JSONDecodeError, KeyError):
                docs = []
        sd = base / "sessions" / label / b.name / "out"
        if not docs and sd.exists():
            docs = [json.loads(f.read_text("utf-8")) for f in sorted(sd.glob("*.json"))]
        for d in docs:
            for x in d.get("decisions") or []:
                out[(d["item_id"], x["slug"])] = x
    return out


def kappa(pairs: Sequence[tuple[Any, Any]]) -> float:
    n = len(pairs)
    if not n:
        return float("nan")
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / n / n
    return (po - pe) / (1 - pe) if pe < 1 else float("nan")


def adjudication(P: Path) -> tuple[dict[tuple[str, str], Row], dict[str, dict[tuple[str, str], Row]]]:
    key = {(k["item_id"], k["slug"]): k for k in read_jsonl(P / "adj" / "key.jsonl")}
    ans = {lab: load_answers(P / "adj", lab) for lab in ADJUDICATORS}
    for lab, a in ans.items():
        missing = [u for u in key if u not in a]
        if missing:
            raise SystemExit(f"{lab}: {len(missing)} candidates without a decision, e.g. {missing[:3]}")
    return key, ans


def cmd_arb_packets(args: argparse.Namespace) -> None:
    P = Path(args.private)
    key, ans = adjudication(P)
    _v, _g, ent = taxonomy()
    meta = {i["item_id"]: i for i in protocol()["items"]}
    a, b = (ans[x] for x in ADJUDICATORS)
    rng = random.Random(SEED + 1)
    split = sorted(u for u in key if a[u]["verdict"] != b[u]["verdict"])
    by_doc: dict[str, list[Row]] = defaultdict(list)
    akey = []
    for d, s in split:
        readers = [(ADJUDICATORS[0], a[(d, s)]), (ADJUDICATORS[1], b[(d, s)])]
        rng.shuffle(readers)
        e = entry_view(ent[s])
        by_doc[d].append({k: e[k] for k in ("slug", "name", "definition", "aliases", "named_instances")} | {
            "readings": [{"reader": f"Reader {n + 1}", **{k: r.get(k) for k in ("verdict", "rule", "quote", "note")}}
                         for n, (_lab, r) in enumerate(readers)]})
        akey.append({"item_id": d, "slug": s, "reader_1": readers[0][0], "reader_2": readers[1][0]})
    out = P / "adj" / "arb"
    if (out / "batches").exists():
        raise SystemExit(f"{out / 'batches'} exists")
    (out / "batches").mkdir(parents=True)
    write_jsonl(out / "key.jsonl", akey)
    src = P / "adj" / "batches"
    first = min(src.iterdir())
    for k, ds in enumerate(batches_of(list(by_doc), {d: meta[d]["chars"] for d in by_doc}, 5, 300_000), 1):
        bd = out / "batches" / f"A{k:02d}"
        (bd / "out").mkdir(parents=True)
        (bd / "work").mkdir()
        for name in ("GUIDE.md", "rubric.md", "catalogue.md", "check.py"):
            shutil.copyfile(first / name, bd / name)
        shutil.copyfile(ADJ_SRC / "ARBITRATION.md", bd / "ARBITRATION.md")
        for d in ds:
            orig = next(b for b in src.iterdir() if (b / "docs" / d).exists())
            dd = bd / "docs" / d
            dd.mkdir(parents=True)
            for f in ("text.txt", "meta.md"):
                shutil.copyfile(orig / "docs" / d / f, dd / f)
            (dd / "candidates.json").write_text(json.dumps(
                {"item_id": d, "candidates": [{"slug": x["slug"]} for x in by_doc[d]]}, indent=1), "utf-8")
            (dd / "arbitrate.json").write_text(json.dumps({"item_id": d, "units": by_doc[d]}, indent=1,
                                                          ensure_ascii=False), "utf-8")
        (bd / "batch.json").write_text(json.dumps({"batch": bd.name, "items": ds}, indent=1), "utf-8")
    print(f"{len(split)} split units on {len(by_doc)} docs, {len(list((out / 'batches').iterdir()))} batches")


# ── the reference ──────────────────────────────────────────────────────────────────────────────


def build_reference(P: Path) -> tuple[list[Row], Row]:
    """ref-t rows (every unit of the universe) and agreement statistics, as PROTOCOL.md."""
    units = read_jsonl(P / "units.jsonl")
    key, ans = adjudication(P)
    a, b = (ans[x] for x in ADJUDICATORS)
    split = [u for u in key if a[u]["verdict"] != b[u]["verdict"]]
    arb = {lab: load_answers(P / "adj" / "arb", lab) for lab in ARBITERS} if split else {x: {} for x in ARBITERS}
    s, f = (arb[x] for x in ARBITERS)
    missing = [u for u in split if u not in s or u not in f]
    if missing:
        raise SystemExit(f"arbitration missing for {len(missing)} units, e.g. {missing[:3]}")
    rows = []
    for u in units:
        k = (u["item_id"], u["slug"])
        sev: list[str] = []
        if k not in key:
            if u["support"] != 3:
                raise SystemExit(f"unit {k} with support {u['support']} was not adjudicated")
            v, how = "in", "3/3"
        else:
            va, vb = a[k]["verdict"], b[k]["verdict"]
            sev = [x["severity"] for x in (a[k], b[k]) if x.get("severity")]
            if va == vb:
                v, how = {"present": "in", "absent": "out"}.get(va, "dont_care"), f"adjudicators {va}"
            else:
                vs, vf = s[k]["verdict"], f[k]["verdict"]
                sev += [x["severity"] for x in (s[k], f[k]) if x.get("severity")]
                v = {"present": "in", "absent": "out"}[vs] if vs == vf and vs in ("present", "absent") else "dont_care"
                how = f"arbiters {vs}" if vs == vf else f"arbiters {vs}/{vf}"
        rows.append({"item_id": u["item_id"], "slug": u["slug"], "verdict": v, "how": how, "support": u["support"],
                     "calibration": bool(key.get(k, {}).get("calibration")), "severity": sorted(sev),
                     "adjudicators": {lab: ans[lab][k]["verdict"] for lab in ADJUDICATORS} if k in key else None})
    pairs = [(a[u]["verdict"], b[u]["verdict"]) for u in sorted(key)]
    binp = [(x == "present", y == "present") for x, y in pairs]
    cal = [u for u in key if key[u]["calibration"]]
    arbp = [(s[u]["verdict"], f[u]["verdict"]) for u in split]
    stats = {"candidates": len(key), "calibration": len(cal),
             "calibration_both_present": sum(a[u]["verdict"] == b[u]["verdict"] == "present" for u in cal),
             "adjudicator_confusion": {f"{x}/{y}": n for (x, y), n in sorted(Counter(pairs).items())},
             "adjudicator_kappa_3way": kappa(pairs), "adjudicator_kappa_present": kappa(binp),
             "adjudicator_agreement_present": sum(x == y for x, y in binp) / max(len(binp), 1),
             "split": len(split), "arbiters_agree": sum(x == y for x, y in arbp),
             "arbiter_confusion": {f"{x}/{y}": n for (x, y), n in sorted(Counter(arbp).items())},
             "counts": {f"{v} <- {h}": n for (v, h), n in sorted(Counter(
                 (r["verdict"], re.sub(r"^arbiters \w+/\w+$", "arbiters split", r["how"])) for r in rows).items())},
             "verdict_by_support": {f"support {k} {v}": n for (k, v), n in sorted(Counter(
                 (r["support"], r["verdict"]) for r in rows).items())},
             "arbiters_agreeing_sided_with": dict(Counter(
                 next((lab for lab in ADJUDICATORS if ans[lab][u]["verdict"] == s[u]["verdict"]), "neither")
                 for u in split if s[u]["verdict"] == f[u]["verdict"] and s[u]["verdict"] != "borderline")),
             "verdicts": dict(Counter(r["verdict"] for r in rows))}
    return rows, stats


def cmd_reference(args: argparse.Namespace) -> None:
    P = Path(args.private)
    rows, stats = build_reference(P)
    write_jsonl(P / "ref-t.jsonl", rows)
    write_jsonl(HERE / "results" / "reference-t.jsonl", rows)
    print(json.dumps(stats, indent=1))


def ref_of(rows: Iterable[Row], dc: str | None = None) -> Ref:
    """item -> slug -> in | dont_care; ``dc`` = 'in' / 'out' maps dont_care to that verdict."""
    ref: Ref = defaultdict(dict)
    for r in rows:
        v = r["verdict"] if r["verdict"] != "dont_care" or dc is None else dc
        if v in ("in", "dont_care"):
            ref[r["item_id"]][r["slug"]] = v
    return ref


def set_ref(sets: Mapping[str, set[str]]) -> Ref:
    return {i: {s: "in" for s in ss} for i, ss in sets.items()}


# ── scoring ────────────────────────────────────────────────────────────────────────────────────


def prf(tp: float, fp: float, fn: float) -> Row:
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else float("nan")
    return {"p": p, "r": r, "f1": f, "tp": tp, "fp": fp, "fn": fn}


def doc_counts(pred: set[str], ref: Mapping[str, str]) -> tuple[int, int, int]:
    """dont_care slugs are removed from both sides."""
    dc = {s for s, v in ref.items() if v == "dont_care"}
    gold = {s for s, v in ref.items() if v == "in"}
    p = pred - dc
    return len(p & gold), len(p - gold), len(gold - p)


Counts = dict[str, tuple[float, float, float]]


def per_doc(reps: Sequence[Mapping[str, set[str]]], ref: Mapping[str, Mapping[str, str]], items: Sequence[str]) -> Counts:
    """item -> (tp, fp, fn) averaged over replicates."""
    out = {}
    for d in items:
        c = [doc_counts(rep[d], ref.get(d, {})) for rep in reps]
        out[d] = tuple(sum(x[k] for x in c) / len(c) for k in range(3))  # type: ignore[assignment]
    return out


def total(pd: Counts, docs: Iterable[str]) -> Row:
    tp = fp = fn = 0.0
    for d in docs:
        a, b, c = pd[d]
        tp, fp, fn = tp + a, fp + b, fn + c
    return prf(tp, fp, fn)


class Boot:
    """Paired document bootstrap within stratum (seeded, percentile)."""

    def __init__(self, items: Sequence[str], strata: Mapping[str, str], n: int, seed: int) -> None:
        by: dict[str, list[str]] = defaultdict(list)
        for d in items:
            by[strata[d]].append(d)
        rng = random.Random(seed)
        self.items = list(items)
        self.samples = [[d for k in sorted(by) for d in rng.choices(by[k], k=len(by[k]))] for _ in range(n)]

    def diff(self, a: Counts, b: Counts, m: str) -> Row:
        point = total(a, self.items)[m] - total(b, self.items)[m]
        xs = sorted(total(a, s)[m] - total(b, s)[m] for s in self.samples)
        xs = [x for x in xs if not math.isnan(x)]
        lo, hi = xs[int(0.025 * len(xs))], xs[min(int(0.975 * len(xs)), len(xs) - 1)]
        return {"d": point, "lo": lo, "hi": hi}

    def level(self, a: Counts, m: str) -> Row:
        xs = sorted(total(a, s)[m] for s in self.samples)
        return {"v": total(a, self.items)[m], "lo": xs[int(0.025 * len(xs))], "hi": xs[int(0.975 * len(xs))]}


def band(chars: int) -> str:
    return next((n for cap, n in BANDS if chars < cap), ">100k")


def pair_f1(a: Mapping[str, set[str]], b: Mapping[str, set[str]], items: Sequence[str]) -> float:
    """Slug F1 of one observation against another (symmetric)."""
    tp = sum(len(a[i] & b[i]) for i in items)
    n = sum(len(a[i]) + len(b[i]) for i in items)
    return 2 * tp / n if n else float("nan")


def ops(src: Sources, name: str) -> Row:
    """Cost, latency, failures of an eval-run arm."""
    obs = [r for rep in src.rows[name] for r in rep.values()]
    lat = sorted(r.get("standalone_latency_ms") or 0 for r in obs if r.get("status") == "ok")
    cost = [r.get("standalone_cost_usd") or 0 for r in obs]
    q = lambda xs, p: xs[min(int(p * len(xs)), len(xs) - 1)] if xs else None
    return {"observations": len(obs), "status": dict(Counter(r.get("status") for r in obs)),
            "ann_status": dict(Counter(r.get("ann_status") for r in obs)),
            "truncated": sum(int((r.get("counts") or {}).get("truncated_docs") or 0) for r in obs),
            "latency_s_p50": statistics.median(lat) / 1000 if lat else None,
            "latency_s_p95": (q(lat, 0.95) or 0) / 1000, "latency_s_max": (lat[-1] / 1000) if lat else None,
            "standalone_usd_per_1k": 1000 * sum(cost) / len(obs), "actual_usd": sum(r.get("cost_usd") or 0 for r in obs),
            "reasoning_tokens_mean": statistics.mean(r.get("reasoning_tokens") or 0 for r in obs),
            "completion_tokens_mean": statistics.mean(r.get("completion_tokens") or 0 for r in obs)}


def cmd_score(args: argparse.Namespace) -> None:
    P = Path(args.private)
    pr = protocol()
    rule = pr["rule"]["confirm_iff"]
    src = Sources(parse_runs(args.run))
    items = src.items
    strata = {i: src.meta[i]["stratum"] for i in items}
    boot = Boot(items, strata, pr["bootstrap"]["resamples"], pr["bootstrap"]["seed"])
    arms = [a for a in ("PROD", "HALL", "BASEF") if a in src.arms]
    preds = {a: src.slugs(a) for a in arms}
    refrows = read_jsonl(P / "ref-t.jsonl") if (P / "ref-t.jsonl").exists() else None
    judges = {j: src.judge_slugs(j) for j in JUDGES}
    majority = {i: {s for s in set().union(*(judges[j][i] for j in JUDGES))
                    if sum(s in judges[j][i] for j in JUDGES) >= 2} for i in items}
    refs: dict[str, Ref] = {f"judge {j}": set_ref(judges[j]) for j in JUDGES}
    refs["majority"] = set_ref(majority)
    if refrows is not None:
        refs = {"ref-t": ref_of(refrows), **refs, "ref-t dc=in": ref_of(refrows, "in"),
                "ref-t dc=out": ref_of(refrows, "out")}
    primary = "ref-t" if refrows is not None else "majority"
    res: Row = {"primary_reference": primary, "arms": {}, "deltas": {}, "ops": {}, "sensitivity": {}}
    pdc = {(r, a): per_doc(preds[a], refs[r], items) for r in refs for a in arms}
    for r in refs:
        res["arms"][r] = {a: {m: boot.level(pdc[(r, a)], m) for m in ("p", "r", "f1")} | {
            "counts": total(pdc[(r, a)], items)} for a in arms}
        res["deltas"][r] = {f"{x} - {y}": {m: boot.diff(pdc[(r, x)], pdc[(r, y)], m) for m in ("f1", "p", "r")}
                            for x, y in (("HALL", "PROD"), ("HALL", "BASEF"), ("BASEF", "PROD"))
                            if x in arms and y in arms}
    for a in arms:
        if a in src.rows:
            res["ops"][a] = ops(src, a)
    # sensitivities on the primary reference
    R = refs[primary]
    sens: Row = {}
    if "HALL" in arms:
        sens["per_replicate"] = {f"HALL r{k + 1} - PROD": boot.diff(per_doc([rep], R, items), pdc[(primary, "PROD")],
                                                                     "f1") for k, rep in enumerate(preds["HALL"])}
        full = total(pdc[(primary, "HALL")], items)["f1"] - total(pdc[(primary, "PROD")], items)["f1"]
        loo = {d: (total(pdc[(primary, "HALL")], [x for x in items if x != d])["f1"]
                   - total(pdc[(primary, "PROD")], [x for x in items if x != d])["f1"]) - full for d in items}
        worst = max(loo, key=lambda d: abs(loo[d]))
        sens["leave_one_out"] = {"largest_change": loo[worst], "item": worst,
                                 "min_delta": full + min(loo.values()), "max_delta": full + max(loo.values())}
        for name, key_of in (("by_stratum", lambda d: strata[d]), ("by_band", lambda d: band(src.meta[d]["chars"]))):
            groups: dict[str, list[str]] = defaultdict(list)
            for d in items:
                groups[key_of(d)].append(d)
            sens[name] = {g: {"n": len(ds), "PROD": total(pdc[(primary, "PROD")], ds)["f1"],
                              "HALL": total(pdc[(primary, "HALL")], ds)["f1"],
                              "d_f1": total(pdc[(primary, "HALL")], ds)["f1"] - total(pdc[(primary, "PROD")], ds)["f1"]}
                          for g, ds in sorted(groups.items())}
        if refrows is not None:
            adv: Ref = {d: dict(v) for d, v in refs[primary].items()}
            favour: Counter = Counter()
            for d in items:
                for s, v in list(adv.get(d, {}).items()):
                    if v != "dont_care":
                        continue
                    rh = sum(s in rep[d] for rep in preds["HALL"]) / len(preds["HALL"])
                    rp = sum(s in rep[d] for rep in preds["PROD"]) / len(preds["PROD"])
                    adv[d][s] = "out" if rh >= rp else "in"
                    favour["HALL predicts more" if rh > rp else "PROD predicts more" if rp > rh else "tie"] += 1
            adv = {d: {s: v for s, v in vs.items() if v != "out"} for d, vs in adv.items()}
            sens["adversarial_dont_care"] = boot.diff(per_doc(preds["HALL"], adv, items),
                                                      per_doc(preds["PROD"], adv, items), "f1")
            sens["dont_care_by_who_predicts"] = dict(favour)
            key, ans = adjudication(P)
            for lab in ADJUDICATORS:
                r1: Ref = defaultdict(dict)
                for r in refrows:
                    k = (r["item_id"], r["slug"])
                    if (k in key and ans[lab][k]["verdict"] == "present") or (k not in key and r["support"] == 3):
                        r1[r["item_id"]][r["slug"]] = "in"
                sens[f"single adjudicator {lab}"] = boot.diff(per_doc(preds["HALL"], r1, items),
                                                              per_doc(preds["PROD"], r1, items), "f1")
    res["sensitivity"] = sens
    # agreement between observations and between judges
    agree: Row = {}
    if "HALL" in arms and len(preds["HALL"]) > 1:
        agree["HALL r1 vs r2"] = pair_f1(preds["HALL"][0], preds["HALL"][1], items)
    if "BASEF" in arms:
        agree["BASEF vs PROD"] = pair_f1(preds["BASEF"][0], preds["PROD"][0], items)
    for x, y in (("opus", "sonnet"), ("opus", "sol"), ("sonnet", "sol")):
        agree[f"judge {x} vs {y}"] = pair_f1(judges[x], judges[y], items)
    res["agreement_slug_f1"] = agree
    res["judge_status"] = {j: dict(c) for j, c in src.judge_status.items()}
    res["invalid_slugs_dropped"] = dict(src.invalid)
    res["mean_slugs_per_doc"] = {**{a: statistics.mean(len(rep[i]) for rep in preds[a] for i in items) for a in arms},
                                 **{f"judge {j}": statistics.mean(len(judges[j][i]) for i in items) for j in JUDGES},
                                 "majority": statistics.mean(len(majority[i]) for i in items)}
    if refrows is not None:
        res["reference"] = build_reference(P)[1]
    if "HALL" in arms:
        d = res["deltas"][primary]["HALL - PROD"]
        judge_d = [res["deltas"][r]["HALL - PROD"]["f1"]["d"] for r in (*(f"judge {j}" for j in JUDGES), "majority")]
        adv = sens["adversarial_dont_care"]["d"] if "adversarial_dont_care" in sens else None
        res["verdict"] = {"reference": primary, **decide(d, res["ops"]["HALL"]["latency_s_p50"], rule, judge_d, adv)}
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "scores.json").write_text(json.dumps(res, indent=1, sort_keys=True, default=float) + "\n", "utf-8")
    (out / "scores.md").write_text(md(res, arms), "utf-8")
    print(md(res, arms))


def decide(d: Mapping[str, Mapping[str, float]], latency_s: float, rule: Mapping[str, float],
           judge_deltas: Sequence[float], adversarial: float | None) -> Row:
    """The frozen rule (PROTOCOL.md): confirms iff dF1 >= delta_f1_min, dP >= delta_precision_min and the median
    latency < the limit (point estimates, ``d`` = {f1, p, r: {d, lo, hi}}). Reading: a confirmation is ``robust`` when
    dF1 > 0 against every judge and the majority (``judge_deltas``) and under the adversarial dont_care assignment (when
    there is one); a non-confirmation is ``refuted`` when dF1's upper bound is below the threshold or the precision or
    latency criterion fails, else ``inconclusive``."""
    ok = {"delta_f1": d["f1"]["d"] >= rule["delta_f1_min"], "delta_p": d["p"]["d"] >= rule["delta_precision_min"],
          "latency": latency_s < rule["hall_median_latency_s_max_exclusive"]}
    confirms = all(ok.values())
    if confirms:
        robust = all(x > 0 for x in judge_deltas) and (adversarial is None or adversarial > 0)
        reading = "robust" if robust else "not robust"
    else:
        refuted = d["f1"]["hi"] < rule["delta_f1_min"] or not ok["delta_p"] or not ok["latency"]
        reading = "refuted" if refuted else "inconclusive"
    return {"criteria": ok, "confirms": confirms, "reading": reading, "delta_f1": dict(d["f1"]),
            "delta_p": dict(d["p"]), "delta_r": dict(d["r"]), "hall_latency_s_p50": latency_s}


def f3(x: Any) -> str:
    return "-" if x is None or math.isnan(x) else f"{x:.3f}"


def ci(x: Mapping[str, float], k: str = "d") -> str:
    return f"{x[k]:+.3f} [{x['lo']:+.3f}, {x['hi']:+.3f}]"


def md(res: Row, arms: Sequence[str]) -> str:
    L = ["# Effort-high confirmation on the test set: scores (`confirm.py score`)", ""]
    if "verdict" in res:
        v = res["verdict"]
        L += [(f"**Verdict (frozen rule, reference {v['reference']}): "
               f"{'CONFIRMS' if v['confirms'] else 'DOES NOT CONFIRM'} ({v['reading']}).** ΔF1 {ci(v['delta_f1'])}, "
               f"ΔP {ci(v['delta_p'])}, ΔR {ci(v['delta_r'])}, HALL median latency {v['hall_latency_s_p50']:.0f} s; "
               f"criteria {v['criteria']}."), ""]
    L += ["## Presence P / R / F1 per reference (HALL: 2 replicates pooled)", "",
          "| reference | " + " | ".join(f"{a} P | {a} R | {a} F1" for a in arms) + " | ΔF1 HALL-PROD [95% CI] | ΔP | ΔR |",
          "|---|" + "---|" * (3 * len(arms) + 3)]
    for r, by in res["arms"].items():
        d = res["deltas"][r].get("HALL - PROD")
        cells = " | ".join(f"{f3(by[a]['p']['v'])} | {f3(by[a]['r']['v'])} | {f3(by[a]['f1']['v'])}" for a in arms)
        L.append(f"| {r} | {cells} | " + (f"{ci(d['f1'])} | {ci(d['p'])} | {ci(d['r'])} |" if d else "- | - | - |"))
    L += ["", "## Other paired differences (F1)", "", "| reference | HALL - BASEF | BASEF - PROD |", "|---|---|---|"]
    for r, ds in res["deltas"].items():
        L.append(f"| {r} | " + " | ".join(ci(ds[k]["f1"]) if k in ds else "-" for k in ("HALL - BASEF", "BASEF - PROD"))
                 + " |")
    L += ["", "## Operations (eval-harness arms)", "",
          ("| arm | obs | status | p50 s | p95 s | max s | $/1k standalone | actual $ | reasoning tok | completion tok | "
           "truncated |"), "|---|---|---|---|---|---|---|---|---|---|---|"]
    for a, o in res["ops"].items():
        L.append(f"| {a} | {o['observations']} | {o['status']} | {o['latency_s_p50']:.0f} | {o['latency_s_p95']:.0f} | "
                 f"{o['latency_s_max']:.0f} | {o['standalone_usd_per_1k']:.2f} | {o['actual_usd']:.3f} | "
                 f"{o['reasoning_tokens_mean']:.0f} | {o['completion_tokens_mean']:.0f} | {o['truncated']} |")
    s = res["sensitivity"]
    if s:
        L += ["", f"## Sensitivity on {res['primary_reference']} (ΔF1 HALL - PROD)", ""]
        for k, v in s.items():
            if isinstance(v, dict) and "d" in v:
                L.append(f"- {k}: {ci(v)}")
            elif k == "per_replicate":
                L += [f"- {kk}: {ci(vv)}" for kk, vv in v.items()]
            elif k == "leave_one_out":
                L.append(f"- leave one document out: largest change {v['largest_change']:+.3f} ({v['item']}); range "
                         f"{v['min_delta']:+.3f} .. {v['max_delta']:+.3f}")
            elif k in ("by_stratum", "by_band"):
                L.append(f"- {k}: " + "; ".join(f"{g} (n {x['n']}) {x['PROD']:.3f} -> {x['HALL']:.3f} ({x['d_f1']:+.3f})"
                                               for g, x in v.items()))
            else:
                L.append(f"- {k}: {v}")
    L += ["", "## Agreement (slug F1 between observations)", ""]
    L += [f"- {k}: {f3(v)}" for k, v in res["agreement_slug_f1"].items()]
    L += ["", "Mean slugs per document: " + ", ".join(f"{k} {v:.2f}" for k, v in res["mean_slugs_per_doc"].items()),
          f"Judge status: {res['judge_status']}; invalid slugs dropped: {res['invalid_slugs_dropped']}"]
    if "reference" in res:
        L += ["", "## Reference ref-t", "", "```", json.dumps(res["reference"], indent=1, default=float), "```"]
    return "\n".join(L) + "\n"


def cmd_units(args: argparse.Namespace) -> None:
    P = Path(args.private)
    src = Sources(parse_runs(args.run))
    units = src.units()
    write_jsonl(P / "units.jsonl", units)
    sup = Counter(u["support"] for u in units)
    print(f"{len(units)} units on {len({u['item_id'] for u in units})} items; judge support {dict(sorted(sup.items()))}; "
          f"invalid slugs dropped {dict(src.invalid)}; judge status {{j: dict(c) for j, c in src.judge_status.items()}}")


def main(argv: Sequence[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["units", "packets", "arb-packets", "reference", "score"])
    ap.add_argument("--private", required=True)
    ap.add_argument("--run", action="append", default=[])
    ap.add_argument("--tasks")
    args = ap.parse_args(argv)
    {"units": cmd_units, "packets": cmd_packets, "arb-packets": cmd_arb_packets, "reference": cmd_reference,
     "score": cmd_score}[args.cmd](args)


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    main()
