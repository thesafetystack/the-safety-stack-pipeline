"""Scoring and analysis of ``stack eval technique_staged`` runs (techpipe D606-D607): one run's summary
(``stack eval score``), a paired comparison of two prediction sources (``stack eval staged-compare``) and one
table over many runs (``stack eval staged-table``). No calls.

**Observations and sources.** A run row (one model, item, repeat) is one *observation*: its payload's technique
rows, or the empty prediction when the row failed (``failed_*``, e.g. an incomplete document without rows,
``failed_permanent``), was skipped (egress) or not attempted (budget, halted). A *source* is a list of
observations: one run's rows of one model (every repeat, or one repeat ``@rK``), several runs' rows pooled, or a
*union* (``union:``) of rows merged per item (per (slug, role) the maximum strength; each slug's quotes from the
observation whose row has the highest strength). Source specs (:func:`load_source`): ``RUN_DIR``,
``RUN_DIR@r2``, ``RUN_DIR#<model>``, ``RUN_DIR@r1#<model>``; several comma-separated parts are pooled;
``union:PART,PART`` merges them per item (``union:RUN_DIR`` = the union of that run's repeats).

**Delivered over all items.** Every metric counts every observation of every gold item: a failed, skipped or
not-attempted row contributes its gold as misses and nothing predicted; a ``partial`` row (``ok`` with the flag)
contributes what it delivered. The counts of failed, partial and not-attempted observations are reported beside
the metrics, and the ``complete_only`` block repeats the primary metric over observations that are ``ok`` and not
partial.

**Gold** (``gold.jsonl`` of the set): rows flagged ``candidate`` (``candidate:<slug>``, entries not in the
taxonomy) are never scored. Rows flagged ``dont_care`` (techpipe D611: a slug an audit judged borderline on that
document) are in no variant and their slug is ignored as a prediction. Gold variants: ``full`` (every other row;
primary), ``both`` (rows whose ``sources`` contain both labellers ``A`` and ``B``), ``high`` (``confidence: high``).
Under ``both`` and ``high`` a predicted slug (cell) that matches a full-gold row outside the variant is ignored:
neither a true nor a false positive; under every variant the same holds for a ``dont_care`` row's slug.

**Scoring on another revision of a set** (:func:`scoring_context`): ``set_dir`` names a set directory other than
the one the runs ran on (e.g. a gold revision with the same items); every run's ``set_items_digest`` must equal the
sha256 of that directory's ``items.jsonl``, else the scoring refuses.

**Metrics** (micro over observations; :func:`score_source`):

* ``slugs`` (primary): slug presence P/R/F1 with group nodes left out on both sides; ``slugs_groups``: with them;
* ``slugs_tolerant`` and ``slugs_levelalt`` (techpipe D626): hierarchy-tolerant slug P/R/F1 (below), overall,
  per stratum and length-weighted (``weighted_tolerant``); the strict metrics are unchanged by them;
* ``cells_ge2``: (slug, role) cells at strength >= 2, group nodes left out;
* ``kinds``: recall and precision per taxonomy kind (intervention, monitoring, assessment);
* ``by_channel_group``, ``by_doc_kind`` (gold), ``by_length``: P/R/F1 per item stratum; length bands
  :data:`LENGTH_BANDS` over ``input.full_text_chars``;
* ``priority_roles``: per role of :data:`PRIORITY_ROLES` and pooled, cell P/R at strength >= 1 and >= 2;
* ``weighted``: length-weighted slug recall and precision: an item weighs ``share(band) / items in band`` with
  the production band shares :data:`LENGTH_SHARES` (default :data:`DEFAULT_SHARES`), so the micro sums estimate
  the production population's;
* ``stages``: stage-wise candidate recall from ``payload.prepass`` (gold slug among the selection's candidates,
  the lexical hits, their union, the sub-menu after the cap, the final rows) and ``outside_submenu`` rows (those
  that are gold: recoveries);
* ``cost``: USD per 1,000 documents actual (``cost_usd``: what this run spent) and standalone
  (``standalone_cost_usd``: every call at its provider-reported cost, cache hits included), the standalone cost
  length-weighted, latency median / p90 per document (actual and standalone), and per stage the calls, the share
  served from the response cache, the provider prompt-cache share and the costs;
* ``repeats``: agreement between repeats of one item (mean pairwise Jaccard of the primary slug sets, share of
  items with identical sets, pairwise slug F1).

**Hierarchy-tolerant slugs** (:func:`tolerant_counts`, techpipe D626). Links ``L`` from the set's taxonomy:
``(t.slug, t.parent)`` for every technique with a parent and ``(c, g.slug)`` for every child of a group node;
``linked(a, b)`` iff ``(a, b)`` or ``(b, a)`` is in ``L`` (no siblings, nothing transitive). A gold row may carry
``level_alternatives: [slug, ...]`` (labellers set it when the right level, group / parent / child / variant, is
genuinely ambiguous): a prediction of any of them is an exact match of that row; the strict metrics ignore the
field, and :func:`load_context` refuses one that is not a slug of the taxonomy. Per observation and gold variant,
with the strict code's kept rows and ignored slugs:

* recall: the gold universe is the strict primary one (kept rows that are not group nodes); every predicted slug
  counts (group nodes included). A gold row earns 1 when its slug or one of its level alternatives is predicted,
  else :data:`TOLERANT_PARTIAL` when a predicted slug is linked to its slug, else 0;
* precision: the predicted slugs, group nodes included (so predicting umbrellas does not buy recall for free),
  less exact matches of ignored rows (``dont_care``, outside the variant; as strict) and of their level
  alternatives. A prediction earns 1 when it is a kept row's slug (group-node rows included) or level
  alternative, else :data:`TOLERANT_PARTIAL` when it is linked to a kept row's slug, else it is ignored when it is
  linked to an ignored row's slug, else 0;
* micro sums over observations of (recall credit, gold rows, precision credit, predicted slugs); F1 from P and R.

``slugs_levelalt`` is the same with link credit 0, and then links play no part at all (no credit and no
linked-ignore rule): strict -> ``slugs_levelalt`` isolates the level alternatives and the predicted group nodes on
the precision side, ``slugs_levelalt`` -> ``slugs_tolerant`` the structural links.

**Stage models** (:func:`stage_models`): the effective model of each call type, from ``run.json``
``stage_models`` (or, for a run made before it was recorded, the pins with the documented default: a stage model
no pin sets is the graded model). Labels name the graded model and, when the mode has them, ``P=`` and ``S=``. The
summary lists per call type the models the rows' calls actually ran on and warns when one differs from the
effective stage model (a fallback, or a run made before the shared cache was isolated by stage model).

**Paired bootstrap** (:func:`compare`): items resampled with replacement (10,000 resamples, seed
:data:`BOOTSTRAP_SEED`), each side's micro metric recomputed over the observations of the drawn items; the 95 %
percentile interval of B - A for slug recall, precision, F1, the length-weighted recall and cells >= 2 F1, and
for the tolerant slug recall, precision, F1 and length-weighted recall (the same draws: adding them moved no
strict interval).
"""

from __future__ import annotations

import datetime as dt
import json
import random
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Row = dict[str, Any]

TASK = "technique_staged"
#: Length bands over ``input.full_text_chars``: name and exclusive upper bound.
LENGTH_BANDS: tuple[tuple[str, int | None], ...] = (("<20k", 20_000), ("20-60k", 60_000), ("60-100k", 100_000),
                                                     (">100k", None))
#: Production shares of the bands (``evals/technique_staged/2026-10-17a/README.md``, "Length weighting";
#: ``scripts/length_bands.py`` over a read-only store copy, 2026-10-17). ``screen_relevant``: documents with a text
#: whose newest relevance-screen verdict is ``relevant`` (12,568 measured; the store carries no ``stack_relevance``
#: verdicts); ``all_text``: every document with a text (43,968 measured); ``brief``: the eval brief's fallback.
LENGTH_SHARES: dict[str, dict[str, float]] = {
    "screen_relevant": {"<20k": 0.0489, "20-60k": 0.2641, "60-100k": 0.4135, ">100k": 0.2736},
    "all_text": {"<20k": 0.4698, "20-60k": 0.1667, "60-100k": 0.2252, ">100k": 0.1383},
    "brief": {"<20k": 0.20, "20-60k": 0.25, "60-100k": 0.28, ">100k": 0.27},
}
DEFAULT_SHARES = "screen_relevant"
PRIORITY_ROLES = ("used", "evaluated", "introduced", "attested_use", "considered", "critiqued")
KINDS = ("intervention", "monitoring", "assessment")
GOLD_VARIANTS = ("full", "both", "high")
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261017
#: Hierarchy-tolerant slugs (techpipe D626): the credit of a prediction linked to a gold row (or of a gold row
#: linked to a prediction) by one structural link of the taxonomy (module docstring).
TOLERANT_PARTIAL = 0.5
UNSCORED = ("skipped", "not_attempted")
#: The call types of each mode, in order (``workflows/annotate/technique_staged/main.py`` ``CALLS``; a test pins
#: the copy): the analysis does not load the stage.
MODE_CALLS: dict[str, list[str]] = {"single": ["single"], "sweep": ["prepass", "sweep"],
                                    "staged": ["prepass", "select", "grade"], "lexical": ["grade"]}


class AnalysisError(Exception):
    """A source or run that cannot be scored (missing files, wrong task, ambiguous model)."""


# ── small helpers ────────────────────────────────────────────────────────────────────────────


def _ratio(a: float, b: float) -> float | None:
    return a / b if b else None


def _prf(tp: float, fp: float, fn: float) -> Row:
    p, r = _ratio(tp, tp + fp), _ratio(tp, tp + fn)
    f1 = (2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else
          (0.0 if p is not None and r is not None else None))
    return {"tp": tp, "fp": fp, "fn": fn, "precision": p, "recall": r, "f1": f1}


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def length_band(chars: int) -> str:
    return next(name for name, hi in LENGTH_BANDS if hi is None or chars < hi)


def read_jsonl(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


# ── taxonomy, gold, items ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Context:
    """What scoring needs besides the observations: gold by item, items by id, kind per slug, group nodes, the
    taxonomy's structural links."""

    gold: Mapping[str, Row]
    items: Mapping[str, Row]
    kind: Mapping[str, str]
    groups: frozenset[str]
    shares: Mapping[str, float] = field(default_factory=lambda: dict(LENGTH_SHARES[DEFAULT_SHARES]))
    shares_name: str = DEFAULT_SHARES
    #: which gold the context holds (``<set dir name>/<gold file>``), for the rendered outputs
    gold_label: str = "gold.jsonl"
    #: ``(child, parent)`` pairs: technique -> its ``parent``, group child -> group (:func:`taxonomy_structure`)
    links: frozenset[tuple[str, str]] = frozenset()

    def linked(self, a: str, b: str) -> bool:
        """One structural link between ``a`` and ``b``, either way (no siblings, nothing transitive)."""
        return (a, b) in self.links or (b, a) in self.links

    def band(self, item_id: str) -> str:
        return length_band(int(self.items[item_id]["input"]["full_text_chars"]))

    def weights(self) -> dict[str, float]:
        """Per gold item: ``share(band) / gold items in band`` (bands with no item: their share is dropped and
        the rest renormalised by the micro ratio itself)."""
        per_band = Counter(self.band(i) for i in self.gold)
        return {i: self.shares[self.band(i)] / per_band[self.band(i)] for i in self.gold}


def taxonomy_structure(path: Path) -> tuple[dict[str, str], frozenset[str], frozenset[tuple[str, str]]]:
    """``(slug -> kind, group-node slugs, links)`` of a taxonomy file (the stage's own loader); links are the
    ``(child, parent)`` pairs ``(t.slug, t.parent)`` of every technique with a parent and ``(c, g.slug)`` of every
    group node's child."""
    from stack.stage_runner import load_path_module

    here = Path(__file__).resolve().parents[2] / "workflows" / "annotate" / "technique_staged" / "taxonomy.py"
    tx = load_path_module("_technique_staged__taxonomy", here)
    if tx is None:  # pragma: no cover - packaging accident
        raise AnalysisError(f"cannot load {here}")
    tax = tx.load_taxonomy(path)
    links = {(str(t.slug), str(t.parent)) for t in tax.techniques if t.parent}
    links |= {(str(c), str(g.slug)) for g in tax.group_nodes for c in g.children}
    return ({t.slug: t.kind for t in tax.techniques}, frozenset(g.slug for g in tax.group_nodes),
            frozenset(links))


def taxonomy_kinds(path: Path) -> tuple[dict[str, str], frozenset[str]]:
    """``(slug -> kind, group-node slugs)`` of a taxonomy file."""
    kind, groups, _links = taxonomy_structure(path)
    return kind, groups


def load_context(set_dir: Path, gold_file: str | None = None, shares: str = DEFAULT_SHARES) -> Context:
    """The set's gold (``gold_file``, default ``gold.jsonl``), items and the taxonomy it was frozen with."""
    from stack.evals.harness import REPO_ROOT

    if shares not in LENGTH_SHARES:
        raise AnalysisError(f"length shares {shares!r}: one of {sorted(LENGTH_SHARES)}")
    gpath = set_dir / (gold_file or "gold.jsonl") if "/" not in (gold_file or "") else Path(str(gold_file))
    if not gpath.is_file():
        raise AnalysisError(f"no gold file {gpath}")
    manifest = json.loads((set_dir / "manifest.json").read_text("utf-8"))
    tax_path = REPO_ROOT / str(manifest["frozen_with"]["taxonomy_path"])
    kind, groups, links = taxonomy_structure(tax_path)
    gold = {g["item_id"]: g for g in read_jsonl(gpath)}
    known = set(kind) | groups
    bad = [f"{iid}:{t.get('slug')}:{x}" for iid, g in sorted(gold.items()) for t in g["label"]["techniques"]
           for x in _level_alternatives(t) if x not in known]
    if bad:
        raise AnalysisError(f"gold level_alternatives not in {manifest['frozen_with']['taxonomy_path']}: "
                            + ", ".join(bad))
    return Context(gold=gold, items={i["item_id"]: i for i in read_jsonl(set_dir / "items.jsonl")},
                   kind=kind, groups=groups, shares=dict(LENGTH_SHARES[shares]), shares_name=shares,
                   gold_label=f"{set_dir.name}/{gpath.name}" if gpath.parent == set_dir else str(gpath),
                   links=links)


def scoring_context(runs: Sequence[Row], set_dir: Path | None, gold_file: str | None,
                    shares: str = DEFAULT_SHARES) -> Context:
    """The context runs are scored in: their own set (they must all name one ``set_dir``), or ``set_dir`` (another
    revision of the set, module docstring), whose ``items.jsonl`` every run must have run on."""
    from stack.evals.score import items_digest_problem

    if set_dir is None:
        sets = {str(r["set_dir"]) for r in runs}
        if len(sets) != 1:
            raise AnalysisError(f"the runs belong to several sets: {sorted(sets)}")
        return load_context(Path(sets.pop()), gold_file, shares)
    for r in runs:
        problem = items_digest_problem(r, set_dir)
        if problem is not None:
            raise AnalysisError(f"run {r.get('run_id')}: {problem}")
    return load_context(set_dir, gold_file, shares)


def gold_rows(g: Row, variant: str) -> tuple[list[Row], set[str]]:
    """``(rows of the variant, slugs ignored as predictions)``: candidate rows never count; ``dont_care`` rows are in
    no variant and, like full-gold rows outside the variant, their slugs are ignored."""
    rows = [t for t in g["label"]["techniques"] if not t.get("candidate")]
    scored = [t for t in rows if not t.get("dont_care")]
    if variant == "full":
        keep = scored
    elif variant == "both":
        keep = [t for t in scored if {"A", "B"} <= set(t.get("sources") or [])]
    elif variant == "high":
        keep = [t for t in scored if t.get("confidence") == "high"]
    else:
        raise AnalysisError(f"gold variant {variant!r}: one of {GOLD_VARIANTS}")
    kept = {str(t["slug"]) for t in keep}
    return keep, {str(t["slug"]) for t in rows} - kept


def _level_alternatives(t: Mapping[str, Any]) -> list[str]:
    """A gold row's ``level_alternatives`` (module docstring, "Hierarchy-tolerant slugs"); malformed: refused."""
    v = t.get("level_alternatives")
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise AnalysisError(f"gold row {t.get('slug')!r}: level_alternatives must be a list of slugs, got {v!r}")
    return list(v)


def tolerant_counts(g: Row, variant: str, pslugs: set[str], ctx: Context,
                    partial: float = TOLERANT_PARTIAL) -> tuple[float, float, float, float]:
    """``(recall credit, gold rows, precision credit, predicted slugs)`` of one observation predicting ``pslugs``
    against item gold ``g`` under ``variant`` (module docstring, "Hierarchy-tolerant slugs"). ``partial`` is the
    credit of one structural link; at 0 links play no part (neither credit nor the linked-ignore rule)."""
    keep, ignore = gold_rows(g, variant)
    rows = [t for t in g["label"]["techniques"] if not t.get("candidate")]
    kept_alts: dict[str, set[str]] = defaultdict(set)
    for t in keep:
        kept_alts[str(t["slug"])].update(_level_alternatives(t))
    ignored_alts = {x for t in rows if str(t["slug"]) in ignore for x in _level_alternatives(t)}
    use_links = partial > 0
    # recall: the strict primary universe (kept rows, group nodes left out) against every predicted slug
    tp_r = 0.0
    universe = [s for s in kept_alts if s not in ctx.groups]
    for s in universe:
        if s in pslugs or kept_alts[s] & pslugs:
            tp_r += 1.0
        elif use_links and any(ctx.linked(p, s) for p in pslugs):
            tp_r += partial
    # precision: predicted slugs, group nodes included, less exact matches of ignored rows (and their alternatives)
    all_alts = {x for alts in kept_alts.values() for x in alts}
    tp_p = 0.0
    n_pred = 0
    for p in pslugs - ignore:
        if p in kept_alts or p in all_alts:
            credit = 1.0
        elif p in ignored_alts:
            continue  # a level alternative of an ignored row: an exact match of it
        elif use_links and any(ctx.linked(p, k) for k in kept_alts):
            credit = partial
        elif use_links and any(ctx.linked(p, i) for i in ignore):
            continue
        else:
            credit = 0.0
        tp_p += credit
        n_pred += 1
    return tp_r, float(len(universe)), tp_p, float(n_pred)


def cells(rows: Iterable[Mapping[str, Any]]) -> dict[tuple[str, str], int]:
    """``(slug, role) -> strength`` over nonzero roles (a repeated slug: the maximum)."""
    out: dict[tuple[str, str], int] = {}
    for r in rows:
        for role, v in (r.get("roles") or {}).items():
            if v:
                k = (str(r["slug"]), str(role))
                out[k] = max(out.get(k, 0), int(v))
    return out


# ── observations ─────────────────────────────────────────────────────────────────────────────


@dataclass
class Obs:
    """One prediction for one item (module docstring)."""

    item_id: str
    repeat: int | str
    status: str
    partial: bool
    rows: list[Row]
    prepass: Row | None
    repairs: list[str]
    cost_usd: float
    standalone_cost_usd: float | None
    latency_ms: float
    standalone_latency_ms: float | None
    calls: list[Row]
    mode: str | None = None
    members: int = 1

    @property
    def scored_ok(self) -> bool:
        return self.status == "ok"


def obs_of(row: Row) -> Obs:
    payload = (row.get("payload") or {}) if row["status"] == "ok" else {}
    return Obs(item_id=row["item_id"], repeat=row.get("repeat", 1), status=str(row["status"]),
               partial="partial" in (payload.get("flags") or []), rows=list(payload.get("techniques") or []),
               prepass=payload.get("prepass") if row["status"] == "ok" else None,
               repairs=[str(x) for x in payload.get("repairs") or []],
               cost_usd=float(row.get("cost_usd") or 0.0),
               standalone_cost_usd=(None if row.get("standalone_cost_usd") is None
                                    else float(row["standalone_cost_usd"])),
               latency_ms=float(row.get("latency_ms") or 0.0),
               standalone_latency_ms=(None if row.get("standalone_latency_ms") is None
                                      else float(row["standalone_latency_ms"])),
               calls=list(row.get("calls") or []), mode=payload.get("mode"))


def union(obs: Sequence[Obs]) -> list[Obs]:
    """One observation per item: per (slug, role) the maximum strength over the item's observations; each
    slug's quotes (and other fields) from the row with the highest strength (first such on a tie); status
    ``ok`` when any member answered, partial when every answering member was partial; costs summed, latency the
    members' maximum (they can run in parallel)."""
    by: dict[str, list[Obs]] = defaultdict(list)
    for o in obs:
        by[o.item_id].append(o)
    out = []
    for iid in sorted(by):
        group = by[iid]
        answered = [o for o in group if o.scored_ok]
        best: dict[str, tuple[int, Row]] = {}
        roles: dict[str, dict[str, int]] = defaultdict(dict)
        for o in answered:
            for r in o.rows:
                slug = str(r["slug"])
                top = max([int(v) for v in (r.get("roles") or {}).values() if v] or [0])
                if slug not in best or top > best[slug][0]:
                    best[slug] = (top, r)
                for role, v in (r.get("roles") or {}).items():
                    if v:
                        roles[slug][role] = max(roles[slug].get(role, 0), int(v))
        rows = [{**best[s][1], "roles": dict(roles[s])} for s in sorted(best)]
        sa = [o.standalone_cost_usd for o in group]
        sl = [o.standalone_latency_ms for o in group]
        out.append(Obs(item_id=iid, repeat="union", status="ok" if answered else group[0].status,
                       partial=bool(answered) and all(o.partial for o in answered), rows=rows, prepass=None,
                       repairs=[x for o in answered for x in o.repairs], cost_usd=sum(o.cost_usd for o in group),
                       standalone_cost_usd=None if any(x is None for x in sa) else sum(x for x in sa if x is not None),
                       latency_ms=max(o.latency_ms for o in group),
                       standalone_latency_ms=None if any(x is None for x in sl) else max(x for x in sl if x is not None),
                       calls=[c for o in group for c in o.calls], mode="union", members=len(group)))
    return out


@dataclass
class Source:
    label: str
    obs: list[Obs]
    runs: list[Row]
    is_union: bool = False


def load_run(run_dir: Path) -> tuple[Row, list[Row]]:
    if not (run_dir / "run.json").is_file() or not (run_dir / "rows.jsonl").is_file():
        raise AnalysisError(f"{run_dir}: not a finished run (run.json and rows.jsonl)")
    run = json.loads((run_dir / "run.json").read_text("utf-8"))
    if run.get("task") != TASK:
        raise AnalysisError(f"{run_dir}: task {run.get('task')!r}, not {TASK}")
    return run, read_jsonl(run_dir / "rows.jsonl")


def stage_models(run: Row, model: str) -> dict[str, str]:
    """The effective model of each call type of graded model ``model`` (module docstring, "Stage models")."""
    recorded = (run.get("stage_models") or {}).get(model)
    if recorded:
        return dict(recorded)
    p = (run.get("pins") or {}).get(model) or {}
    mode = p.get("mode") or run.get("mode")
    graded = str(p.get("model") or model)
    keys = {"prepass": "prepass_model", "select": "select_model"}
    return {c: str(p.get(keys[c]) or graded) if c in keys else graded for c in MODE_CALLS.get(str(mode), [])}


def run_label(run: Row, model: str) -> str:
    p = (run.get("pins") or {}).get(model) or {}
    mode = p.get("mode") or run.get("mode") or "?"
    parts = [f"{mode}", model.split("/")[-1]]
    sm = stage_models(run, model)
    for c, tag in (("prepass", "P"), ("select", "S")):
        if c in sm:
            parts.append(f"{tag}={sm[c].split('/')[-1]}")
    return " ".join(parts)


def observed_models(rows: Sequence[Row]) -> dict[str, dict[str, int]]:
    """Per call type, how many of the rows' calls ran on each model (``calls[].model``)."""
    out: dict[str, Counter[str]] = defaultdict(Counter)
    for r in rows:
        for c in r.get("calls") or []:
            if c.get("model"):
                out[str(c["stage"])][str(c["model"])] += 1
    return {st: dict(cnt.most_common()) for st, cnt in out.items()}


def stage_model_warnings(sm: Mapping[str, str], observed: Mapping[str, Mapping[str, int]]) -> list[str]:
    return [f"{st} ran on {m} x{n}, pinned {sm[st]} (a fallback, or another model's cached answer)"
            for st, seen in observed.items() if st in sm for m, n in seen.items() if m != sm[st]]


def _part(spec: str) -> tuple[Path, int | None, str | None]:
    model = None
    if "#" in spec:
        spec, model = spec.split("#", 1)
    repeat = None
    if "@r" in spec:
        spec, r = spec.rsplit("@r", 1)
        if not r.isdigit():
            raise AnalysisError(f"source part {spec}@r{r}: the repeat must be a number")
        repeat = int(r)
    return Path(spec), repeat, model


def load_source(spec: str) -> Source:
    """A source from its spec (module docstring)."""
    is_union = spec.startswith("union:")
    body = spec.removeprefix("union:")
    obs: list[Obs] = []
    runs: list[Row] = []
    labels = []
    for part in [p for p in body.split(",") if p.strip()]:
        d, repeat, model = _part(part.strip())
        run, rows = load_run(d)
        models = list(run["models"])
        if model is None:
            if len(models) != 1:
                raise AnalysisError(f"{d}: models {models}: name one with {d}#<model>")
            model = models[0]
        if model not in models:
            raise AnalysisError(f"{d}: model {model!r} is not one of the run's {models}")
        mine = [r for r in rows if r["model"] == model and (repeat is None or int(r["repeat"]) == repeat)]
        if not mine:
            raise AnalysisError(f"{part}: no rows")
        obs += [obs_of(r) for r in mine]
        runs.append({**run, "_dir": str(d), "_model": model})
        labels.append(run_label(run, model) + (f" r{repeat}" if repeat is not None else ""))
    if not obs:
        raise AnalysisError(f"source {spec!r}: no parts")
    if is_union:
        obs = union(obs)
    label = (f"union({' + '.join(labels)})" if is_union else " + ".join(labels))
    return Source(label=label, obs=obs, runs=runs, is_union=is_union)


# ── metrics ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class ItemCounts:
    """Per observation: the confusion counts each metric sums (also what the bootstrap resamples)."""

    item_id: str
    slugs: tuple[int, int, int]
    slugs_groups: tuple[int, int, int]
    cells_ge2: tuple[int, int, int]
    kinds: dict[str, tuple[int, int, int]]
    roles: dict[tuple[str, int], tuple[int, int, int]]
    #: :func:`tolerant_counts` at :data:`TOLERANT_PARTIAL` and at 0 (level alternatives only)
    slugs_tolerant: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    slugs_levelalt: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)


def item_counts(o: Obs, ctx: Context, variant: str) -> ItemCounts:
    g = ctx.gold[o.item_id]
    keep, outside = gold_rows(g, variant)
    pred_rows = o.rows if o.scored_ok else []
    gslugs = {str(t["slug"]) for t in keep}
    pslugs = {str(t["slug"]) for t in pred_rows}

    def conf(pred: set[Any], gold: set[Any], ignore: set[Any]) -> tuple[int, int, int]:
        return len(pred & gold), len(pred - gold - ignore), len(gold - pred)

    grp = ctx.groups
    slugs = conf({s for s in pslugs if s not in grp}, {s for s in gslugs if s not in grp}, outside)
    slugs_groups = conf(pslugs, gslugs, outside)
    gc, pc = cells(keep), cells(pred_rows)
    ignore_cells = {c for c in pc if c[0] in outside}
    ge2 = conf({c for c, v in pc.items() if v >= 2 and c[0] not in grp},
               {c for c, v in gc.items() if v >= 2 and c[0] not in grp}, ignore_cells)
    kinds = {}
    for k in KINDS:
        kinds[k] = conf({s for s in pslugs if ctx.kind.get(s) == k}, {s for s in gslugs if ctx.kind.get(s) == k},
                        outside)
    roles: dict[tuple[str, int], tuple[int, int, int]] = {}
    for t in (1, 2):
        for role in (*PRIORITY_ROLES, "any"):
            want = PRIORITY_ROLES if role == "any" else (role,)
            roles[(role, t)] = conf({c for c, v in pc.items() if c[1] in want and v >= t and c[0] not in grp},
                                    {c for c, v in gc.items() if c[1] in want and v >= t and c[0] not in grp},
                                    ignore_cells)
    return ItemCounts(o.item_id, slugs, slugs_groups, ge2, kinds, roles,
                      slugs_tolerant=tolerant_counts(g, variant, pslugs, ctx),
                      slugs_levelalt=tolerant_counts(g, variant, pslugs, ctx, partial=0.0))


def _sum(triples: Iterable[tuple[int, int, int]], weights: Iterable[float] | None = None) -> Row:
    tp = fp = fn = 0.0
    ws = iter(weights) if weights is not None else None
    for a, b, c in triples:
        w = next(ws) if ws is not None else 1.0
        tp, fp, fn = tp + w * a, fp + w * b, fn + w * c
    return _prf(tp, fp, fn)


def _tol_prf(tp_r: float, n_gold: float, tp_p: float, n_pred: float) -> Row:
    p, r = _ratio(tp_p, n_pred), _ratio(tp_r, n_gold)
    f1 = (2 * p * r / (p + r) if p is not None and r is not None and p + r > 0 else
          (0.0 if p is not None and r is not None else None))
    return {"tp_recall": tp_r, "gold": n_gold, "tp_precision": tp_p, "predicted": n_pred, "precision": p,
            "recall": r, "f1": f1}


def _sum_tol(quads: Iterable[tuple[float, float, float, float]], weights: Iterable[float] | None = None) -> Row:
    """Micro sums of :func:`tolerant_counts` (optionally weighted per observation) and their P / R / F1."""
    acc = [0.0, 0.0, 0.0, 0.0]
    ws = iter(weights) if weights is not None else None
    for q in quads:
        w = next(ws) if ws is not None else 1.0
        for j in range(4):
            acc[j] += w * q[j]
    return _tol_prf(*acc)


def stage_block(obs: Sequence[Obs], ctx: Context) -> Row | None:
    """Stage-wise candidate recall (module docstring) over answered observations with a pre-pass record."""
    with_pre = [o for o in obs if o.scored_ok and o.prepass]
    if not with_pre:
        return None
    hit: Counter[str] = Counter()
    gold_n = 0
    outside = recovered = 0
    modes = {o.mode for o in with_pre}
    #: which candidate stages a mode has (single and sweep: only the final rows)
    applicable = {"selection": "staged" in modes, "lexical": bool(modes & {"staged", "lexical"}),
                  "union": "staged" in modes, "submenu": bool(modes & {"staged", "lexical"}), "final": True}
    for o in with_pre:
        pre = o.prepass or {}
        keep, _ = gold_rows(ctx.gold[o.item_id], "full")
        g = {str(t["slug"]) for t in keep if str(t["slug"]) not in ctx.groups}
        sel = {str(c["slug"]) for m in pre.get("mentions") or [] for c in m.get("candidates") or []}
        sel |= {str(d["slug"]) for d in pre.get("doc_level") or []}
        lex = {str(x["slug"]) for x in pre.get("lexical") or []}
        sub = {str(x) for x in pre.get("submenu") or []}
        final = {str(t["slug"]) for t in o.rows}
        gold_n += len(g)
        for name, got in (("selection", sel), ("lexical", lex), ("union", sel | lex), ("submenu", sub),
                          ("final", final)):
            hit[name] += len(g & got)
        out_rows = {x.split(":", 1)[1] for x in o.repairs if x.startswith("outside_submenu:")}
        outside += len(out_rows)
        recovered += len(out_rows & g)
    return {"observations": len(with_pre), "gold": gold_n,
            "recall": {k: (_ratio(hit[k], gold_n) if applicable[k] else None)
                       for k in ("selection", "lexical", "union", "submenu", "final")},
            "applicable": applicable, "outside_submenu_rows": outside, "outside_submenu_gold": recovered,
            "mentions_per_doc": _ratio(sum(len((o.prepass or {}).get("mentions") or []) for o in with_pre),
                                       len(with_pre)),
            "submenu_per_doc": _ratio(sum(len((o.prepass or {}).get("submenu") or []) for o in with_pre),
                                      len(with_pre))}


def cost_block(obs: Sequence[Obs], ctx: Context) -> Row:
    n = len(obs)
    sa = [o.standalone_cost_usd for o in obs]
    sa_known = [x for x in sa if x is not None]
    by_band: dict[str, list[float]] = defaultdict(list)
    for o in obs:
        if o.standalone_cost_usd is not None and o.item_id in ctx.items:
            by_band[ctx.band(o.item_id)].append(o.standalone_cost_usd)
    present = {b: sum(v) / len(v) for b, v in by_band.items() if v}
    share = sum(ctx.shares[b] for b in present)
    weighted = (sum(ctx.shares[b] * present[b] for b in present) / share * 1000) if share else None
    lat = [o.latency_ms for o in obs if o.status != "skipped" and o.status != "not_attempted"]
    sl = [o.standalone_latency_ms for o in obs if o.standalone_latency_ms is not None]
    stages: dict[str, Row] = {}
    for o in obs:
        for c in o.calls:
            st = stages.setdefault(c["stage"], {"calls": 0, "from_response_cache": 0, "failed": 0, "prompt_tokens": 0,
                                                "cached_tokens": 0, "cost_usd": 0.0, "call_cost_usd": 0.0})
            st["calls"] += 1
            st["from_response_cache"] += bool(c.get("from_response_cache"))
            st["failed"] += c.get("status") != "ok"
            st["prompt_tokens"] += int(c.get("prompt_tokens") or 0)
            st["cached_tokens"] += int(c.get("cached_tokens") or 0)
            st["cost_usd"] += float(c.get("cost_usd") or 0.0)
            st["call_cost_usd"] += float(c.get("call_cost_usd") or 0.0)
    for st in stages.values():
        st["response_cache_share"] = _ratio(st["from_response_cache"], st["calls"])
        st["prompt_cache_share"] = _ratio(st["cached_tokens"], st["prompt_tokens"])
    return {"observations": n, "actual_usd": sum(o.cost_usd for o in obs),
            "usd_per_1000_actual": (sum(o.cost_usd for o in obs) / n * 1000) if n else None,
            "usd_per_1000_standalone": (sum(sa_known) / len(sa_known) * 1000) if sa_known else None,
            "standalone_missing": sum(x is None for x in sa),
            "usd_per_1000_standalone_length_weighted": weighted,
            "length_weighted_bands": sorted(present),
            "latency_ms_median": _quantile(lat, 0.5), "latency_ms_p90": _quantile(lat, 0.9),
            "standalone_latency_ms_median": _quantile(sl, 0.5), "standalone_latency_ms_p90": _quantile(sl, 0.9),
            "per_stage": dict(sorted(stages.items()))}


def repeat_block(obs: Sequence[Obs], ctx: Context) -> Row | None:
    by: dict[str, dict[Any, set[str]]] = defaultdict(dict)
    for o in obs:
        if o.status in UNSCORED:
            continue
        by[o.item_id][o.repeat] = {str(t["slug"]) for t in o.rows if str(t["slug"]) not in ctx.groups}
    multi = {i: v for i, v in by.items() if len(v) >= 2}
    if not multi:
        return None
    jac, same, tp, fpn = [], 0, 0, 0
    for v in multi.values():
        sets = list(v.values())
        same += all(s == sets[0] for s in sets)
        for a in range(len(sets)):
            for b in range(a + 1, len(sets)):
                x, y = sets[a], sets[b]
                jac.append(1.0 if not x and not y else len(x & y) / len(x | y))
                tp += len(x & y)
                fpn += len(x ^ y)
    return {"items": len(multi), "identical_share": same / len(multi), "mean_jaccard": sum(jac) / len(jac),
            "pairwise_f1": _ratio(2 * tp, 2 * tp + fpn)}


def score_source(obs: Sequence[Obs], ctx: Context) -> Row:
    """Every metric of one source (module docstring)."""
    scored = [o for o in obs if o.item_id in ctx.gold]
    status = Counter(o.status for o in scored)
    weights = ctx.weights()
    out: Row = {
        "observations": len(scored), "items": len({o.item_id for o in scored}),
        "missing_gold_items": sorted({o.item_id for o in obs if o.item_id not in ctx.gold}),
        "status": dict(status), "failed": sum(o.status.startswith("failed") for o in scored),
        "partial": sum(o.scored_ok and o.partial for o in scored),
        "not_attempted": sum(o.status in UNSCORED for o in scored),
        "length_shares": ctx.shares_name, "tolerant_partial": TOLERANT_PARTIAL, "variants": {},
    }
    for variant in GOLD_VARIANTS:
        counts = [item_counts(o, ctx, variant) for o in scored]
        w = [weights[c.item_id] for c in counts]
        v: Row = {
            "slugs": _sum(c.slugs for c in counts), "slugs_groups": _sum(c.slugs_groups for c in counts),
            "cells_ge2": _sum(c.cells_ge2 for c in counts),
            "weighted": _sum((c.slugs for c in counts), w),
            "slugs_tolerant": _sum_tol(c.slugs_tolerant for c in counts),
            "slugs_levelalt": _sum_tol(c.slugs_levelalt for c in counts),
            "weighted_tolerant": _sum_tol((c.slugs_tolerant for c in counts), w),
            "kinds": {k: _sum(c.kinds[k] for c in counts) for k in KINDS},
            "priority_roles": {f"{role}>={t}": _sum(c.roles[(role, t)] for c in counts)
                               for t in (1, 2) for role in (*PRIORITY_ROLES, "any")},
        }
        if variant == "full":
            complete = [c for c, o in zip(counts, scored, strict=True) if o.scored_ok and not o.partial]
            v["complete_only"] = {"observations": len(complete), "slugs": _sum(c.slugs for c in complete)}
            for name, key in (("by_channel_group", lambda i: ctx.items[i]["stratum"]["channel_group"]),
                              ("by_doc_kind", lambda i: ctx.gold[i].get("doc_kind") or "?"),
                              ("by_length", ctx.band)):
                groups: dict[str, list[ItemCounts]] = defaultdict(list)
                for c in counts:
                    groups[key(c.item_id)].append(c)
                v[name] = {k: {"observations": len(cs), "slugs": _sum(c.slugs for c in cs),
                               "slugs_tolerant": _sum_tol(c.slugs_tolerant for c in cs),
                               "slugs_levelalt": _sum_tol(c.slugs_levelalt for c in cs)}
                           for k, cs in sorted(groups.items())}
        out["variants"][variant] = v
    out["stages"] = stage_block(scored, ctx)
    out["cost"] = cost_block(scored, ctx)
    out["repeats"] = repeat_block(scored, ctx)
    return out


# ── paired bootstrap ─────────────────────────────────────────────────────────────────────────

#: The metrics :func:`compare` bootstraps: name -> (which counts, which value, weighted?).
BOOT_METRICS = {"slug_recall": ("slugs", "recall", False), "slug_precision": ("slugs", "precision", False),
                "slug_f1": ("slugs", "f1", False), "weighted_recall": ("slugs", "recall", True),
                "cells_ge2_f1": ("cells_ge2", "f1", False),
                "tolerant_recall": ("slugs_tolerant", "recall", False),
                "tolerant_precision": ("slugs_tolerant", "precision", False),
                "tolerant_f1": ("slugs_tolerant", "f1", False),
                "tolerant_weighted_recall": ("slugs_tolerant", "recall", True)}


def _per_item(counts: Sequence[ItemCounts], key: str) -> dict[str, list[float]]:
    width = 4 if key == "slugs_tolerant" else 3
    out: dict[str, list[float]] = defaultdict(lambda: [0.0] * width)
    for c in counts:
        t = getattr(c, key)
        acc = out[c.item_id]
        for j in range(width):
            acc[j] += t[j]
    return out


def _metric(acc: Sequence[float], what: str) -> float | None:
    """``what`` of summed confusion counts ``(tp, fp, fn)`` or tolerant credits (4 values, :func:`_tol_prf`)."""
    v = (_tol_prf(*acc) if len(acc) == 4 else _prf(*acc))[what]
    return None if v is None else float(v)


def compare(a: Source, b: Source, ctx: Context, *, variant: str = "full", resamples: int = BOOTSTRAP_RESAMPLES,
            seed: int = BOOTSTRAP_SEED) -> Row:
    """Paired document bootstrap of B - A (module docstring) over the gold items both sources cover."""
    items = sorted({o.item_id for o in a.obs} & {o.item_id for o in b.obs} & set(ctx.gold))
    if not items:
        raise AnalysisError("the two sources share no gold item")
    only = sorted(({o.item_id for o in a.obs} ^ {o.item_id for o in b.obs}) & set(ctx.gold))
    weights = ctx.weights()
    sides = {}
    for name, src in (("a", a), ("b", b)):
        counts = [item_counts(o, ctx, variant) for o in src.obs if o.item_id in items]
        sides[name] = {k: _per_item(counts, k) for k in ("slugs", "cells_ge2", "slugs_tolerant")}
    rng = random.Random(seed)
    n = len(items)

    def value(side: str, key: str, what: str, weighted: bool, draw: Mapping[str, int]) -> float | None:
        acc = [0.0] * (4 if key == "slugs_tolerant" else 3)
        for iid, k in draw.items():
            c = sides[side][key][iid]
            w = k * (weights[iid] if weighted else 1.0)
            for j in range(len(acc)):
                acc[j] += w * c[j]
        return _metric(acc, what)

    full = dict.fromkeys(items, 1)
    point = {}
    for m, (key, what, wtd) in BOOT_METRICS.items():
        va, vb = value("a", key, what, wtd, full), value("b", key, what, wtd, full)
        point[m] = {"a": va, "b": vb, "diff": None if va is None or vb is None else vb - va}
    diffs: dict[str, list[float]] = {m: [] for m in BOOT_METRICS}
    for _ in range(resamples):
        draw = Counter(items[rng.randrange(n)] for _ in range(n))
        for m, (key, what, wtd) in BOOT_METRICS.items():
            va, vb = value("a", key, what, wtd, draw), value("b", key, what, wtd, draw)
            if va is not None and vb is not None:
                diffs[m].append(vb - va)
    out = {}
    for m, ds in diffs.items():
        out[m] = {**point[m], "ci95": [_quantile(ds, 0.025), _quantile(ds, 0.975)] if ds else None,
                  "share_b_better": _ratio(sum(d > 0 for d in ds), len(ds)), "resamples": len(ds)}
    return {"a": a.label, "b": b.label, "variant": variant, "gold": ctx.gold_label, "items": n,
            "items_in_one_only": only,
            "observations": {"a": sum(o.item_id in items for o in a.obs), "b": sum(o.item_id in items for o in b.obs)},
            "resamples": resamples, "seed": seed, "length_shares": ctx.shares_name,
            "tolerant_partial": TOLERANT_PARTIAL, "metrics": out}


# ── rendering ────────────────────────────────────────────────────────────────────────────────


def _f(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:.{digits}f}"


def _prf_cell(x: Row) -> str:
    return f"{_f(x['precision'])} / {_f(x['recall'])} / {_f(x['f1'])}"


def _usd(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def _table(head: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    return ["| " + " | ".join(head) + " |", "|" + "---|" * len(head), *("| " + " | ".join(r) + " |" for r in rows)]


def source_markdown(label: str, s: Row) -> list[str]:
    """One source's block (the metrics of :func:`score_source`)."""
    full = s["variants"]["full"]
    lines = [f"### {label}", "",
             (f"{s['observations']} observations over {s['items']} items (delivered over all: failed "
              f"{s['failed']}, partial {s['partial']}, skipped / not attempted {s['not_attempted']}; status "
              f"{s['status']}). Slugs = slug presence without group nodes (primary)."), ""]
    rows = []
    for v in GOLD_VARIANTS:
        x = s["variants"][v]
        rows.append([v, _prf_cell(x["slugs"]), _f(x["weighted"]["recall"]), _f(x["weighted"]["precision"]),
                     _prf_cell(x["slugs_groups"]), _prf_cell(x["cells_ge2"])])
    lines += _table(["gold", "slugs P / R / F1", f"length-weighted R ({s['length_shares']})", "weighted P",
                     "with group nodes P / R / F1", "cells >= 2 P / R / F1"], rows) + [""]
    lines += [(f"Hierarchy-tolerant slugs (D626): a level alternative counts as an exact match; one structural link "
               f"(technique parent, group child) earns {s.get('tolerant_partial', TOLERANT_PARTIAL)}; predicted group "
               f"nodes count on the precision side. Level alternatives = the same without link credit."), ""]
    lines += _table(["gold", "slugs P / R / F1", "level alternatives P / R / F1", "tolerant P / R / F1",
                     f"tolerant length-weighted R ({s['length_shares']})", "tolerant weighted P"],
                    [[v, _prf_cell(s["variants"][v]["slugs"]), _prf_cell(s["variants"][v]["slugs_levelalt"]),
                      _prf_cell(s["variants"][v]["slugs_tolerant"]),
                      _f(s["variants"][v]["weighted_tolerant"]["recall"]),
                      _f(s["variants"][v]["weighted_tolerant"]["precision"])] for v in GOLD_VARIANTS]) + [""]
    co = full["complete_only"]
    lines += [f"Complete documents only ({co['observations']} observations): slugs {_prf_cell(co['slugs'])}.", ""]
    lines += _table(["kind", "P / R / F1"], [[k, _prf_cell(full["kinds"][k])] for k in KINDS]) + [""]
    pr = full["priority_roles"]
    lines += _table(["role", ">= 1 P / R", ">= 2 P / R"],
                    [[r, f"{_f(pr[f'{r}>=1']['precision'])} / {_f(pr[f'{r}>=1']['recall'])}",
                      f"{_f(pr[f'{r}>=2']['precision'])} / {_f(pr[f'{r}>=2']['recall'])}"]
                     for r in (*PRIORITY_ROLES, "any")]) + [""]
    for name, title in (("by_channel_group", "channel group"), ("by_doc_kind", "gold doc_kind"),
                        ("by_length", "length band")):
        lines += _table([title, "obs", "slugs P / R / F1", "level alternatives P / R / F1", "tolerant P / R / F1"],
                        [[k, str(v["observations"]), _prf_cell(v["slugs"]), _prf_cell(v["slugs_levelalt"]),
                          _prf_cell(v["slugs_tolerant"])] for k, v in full[name].items()]) + [""]
    st = s.get("stages")
    if st:
        r = st["recall"]
        lines += [(f"Stage-wise candidate recall (full gold, no group nodes; {st['observations']} observations, "
                   f"{st['gold']} gold slugs): selection {_f(r['selection'])}, lexical {_f(r['lexical'])}, union "
                   f"{_f(r['union'])}, sub-menu after cap {_f(r['submenu'])}, final {_f(r['final'])}; rows outside "
                   f"the sub-menu {st['outside_submenu_rows']} (gold: {st['outside_submenu_gold']}); notes / doc "
                   f"{_f(st['mentions_per_doc'], 1)}, sub-menu / doc {_f(st['submenu_per_doc'], 1)}."), ""]
    c = s["cost"]
    lines += [(f"Cost: USD / 1,000 docs actual {_usd(c['usd_per_1000_actual'])}, standalone "
               f"{_usd(c['usd_per_1000_standalone'])} (length-weighted {_usd(c['usd_per_1000_standalone_length_weighted'])})"
               f"; latency ms p50 / p90 actual {_f(c['latency_ms_median'], 0)} / {_f(c['latency_ms_p90'], 0)}, "
               f"standalone {_f(c['standalone_latency_ms_median'], 0)} / {_f(c['standalone_latency_ms_p90'], 0)}."), ""]
    if c["per_stage"]:
        lines += _table(["stage", "calls", "from response cache", "failed", "prompt-cache share", "actual USD",
                         "standalone USD"],
                        [[k, str(v["calls"]), _f(v["response_cache_share"], 2), str(v["failed"]),
                          _f(v["prompt_cache_share"], 2), f"{v['cost_usd']:.4f}", f"{v['call_cost_usd']:.4f}"]
                         for k, v in c["per_stage"].items()]) + [""]
    rp = s.get("repeats")
    if rp:
        lines += [(f"Repeat agreement ({rp['items']} items): identical slug sets {_f(rp['identical_share'], 2)}, "
                   f"mean Jaccard {_f(rp['mean_jaccard'])}, pairwise F1 {_f(rp['pairwise_f1'])}."), ""]
    return lines


def compare_markdown(c: Row) -> str:
    lines = [f"# technique_staged paired bootstrap ({c['variant']} gold `{c.get('gold', 'gold.jsonl')}`)", "",
             f"A = {c['a']}", f"B = {c['b']}", "",
             (f"{c['items']} items ({c['observations']['a']} / {c['observations']['b']} observations); "
              f"{c['resamples']:,} resamples of items, seed {c['seed']}; length shares {c['length_shares']}; "
              f"tolerant_* = hierarchy-tolerant slugs, link credit "
              f"{c.get('tolerant_partial', TOLERANT_PARTIAL)} (D626)."
              + (f" Items in one source only (left out): {', '.join(c['items_in_one_only'])}."
                 if c["items_in_one_only"] else "")), ""]
    rows = [[m, _f(v["a"]), _f(v["b"]), _f(v["diff"]),
             f"[{_f(v['ci95'][0])}, {_f(v['ci95'][1])}]" if v["ci95"] else "n/a", _f(v["share_b_better"], 3)]
            for m, v in c["metrics"].items()]
    lines += _table(["metric", "A", "B", "B - A", "95 % CI", "share of resamples B > A"], rows)
    return "\n".join(lines)


# ── one run's summary (``stack eval score``) ─────────────────────────────────────────────────


def summarize(run: Row, rows: list[Row], gold: Mapping[str, Row], set_dir: Path, gold_file: str | None,
              usage: Row | None = None, shares: str = DEFAULT_SHARES) -> Row:
    """``stack eval score`` for a technique_staged run: per model the harness's general block, then the
    metrics of every repeat, of all repeats pooled and of the union of the repeats (two or more)."""
    from stack.evals.score import cache_block, general_block

    ctx = load_context(set_dir, gold_file, shares)
    per: dict[str, Row] = {}
    warnings = list(run.get("warnings") or [])
    for m in run["models"]:
        mr = [r for r in rows if r["model"] == m]
        obs = [obs_of(r) for r in mr]
        repeats = sorted({int(r["repeat"]) for r in mr})
        blocks: dict[str, Row] = {"pooled": score_source(obs, ctx)}
        if len(repeats) > 1:
            for k in repeats:
                blocks[f"r{k}"] = score_source([o for o in obs if o.repeat == k], ctx)
            blocks["union"] = score_source(union(obs), ctx)

        def key(r: Row) -> str:
            payload = (r.get("payload") or {}) if r["status"] == "ok" else {}
            return json.dumps(sorted(str(t["slug"]) for t in payload.get("techniques") or []))

        sm, seen = stage_models(run, m), observed_models(mr)
        warnings += [f"{m}: {w}" for w in stage_model_warnings(sm, seen)]
        per[m] = {"label": run_label(run, m), "general": general_block(mr, key, categorical=False),
                  "cache": cache_block(mr), "stage_models": sm, "observed_models": seen, "sources": blocks}
    out = {"run_id": run["run_id"], "task": TASK, "set": run["set"], "split": run["split"],
           "models": list(run["models"]), "mode": run.get("mode"), "gold": bool(gold), "gold_items": len(gold),
           "gold_file": gold_file, "gold_label": ctx.gold_label, "set_dir": str(set_dir), "length_shares": shares,
           "per_model": per, "usage": usage or {},
           "warnings": warnings}
    out["markdown"] = summary_markdown(run, out)
    return out


def summary_markdown(run: Row, s: Row) -> str:
    lines = [f"# stack eval technique_staged: {s['set']} / {s['split']} (mode {s.get('mode')})", "",
             f"Run `{s['run_id']}`; gold `{s.get('gold_label', s['gold_file'])}` ({s['gold_items']} items)."]
    lines += [f"**warning**: {w}" for w in s["warnings"]] + [""]
    for m, b in s["per_model"].items():
        g = b["general"]
        seen = "; ".join(f"{st} " + ", ".join(f"{x.split('/')[-1]} x{n}" for x, n in ms.items())
                         for st, ms in b["observed_models"].items())
        lines += [f"## {b['label']}", "",
                  (f"Rows {g['rows']} (answered {g['answered']}; status {g['status']}); settled "
                   f"${g['settled_usd']:.4f}; schema degraded {g['schema_degraded']}."),
                  ("Stage models: " + ", ".join(f"{st} {x}" for st, x in b["stage_models"].items())
                   + f"; observed in calls: {seen or 'none'}."), ""]
        for name, src in b["sources"].items():
            lines += source_markdown(f"{name}", src)
    return "\n".join(lines)


# ── many runs (``stack eval staged-table``) ──────────────────────────────────────────────────

TABLE_HEAD = ["source", "obs", "failed / partial / n.a.", "slugs P / R / F1", "tolerant P / R / F1", "weighted R",
              "R both", "R high",
              "cells>=2 F1", "+groups F1", "priority R>=2", "$/1k actual", "$/1k standalone", "$/1k sa. weighted",
              "latency p50 / p90 s"]


def table_row(label: str, s: Row) -> list[str]:
    v = s["variants"]
    c = s["cost"]
    lat = (f"{_f(c['latency_ms_median'] / 1000 if c['latency_ms_median'] is not None else None, 0)} / "
           f"{_f(c['latency_ms_p90'] / 1000 if c['latency_ms_p90'] is not None else None, 0)}")
    return [label, str(s["observations"]), f"{s['failed']} / {s['partial']} / {s['not_attempted']}",
            _prf_cell(v["full"]["slugs"]), _prf_cell(v["full"]["slugs_tolerant"]), _f(v["full"]["weighted"]["recall"]),
            _f(v["both"]["slugs"]["recall"]),
            _f(v["high"]["slugs"]["recall"]), _f(v["full"]["cells_ge2"]["f1"]), _f(v["full"]["slugs_groups"]["f1"]),
            _f(v["full"]["priority_roles"]["any>=2"]["recall"]), _usd(c["usd_per_1000_actual"]),
            _usd(c["usd_per_1000_standalone"]), _usd(c["usd_per_1000_standalone_length_weighted"]), lat]


def results_table(run_dirs: Sequence[Path], *, unions: bool = True, gold_file: str | None = None,
                  shares: str = DEFAULT_SHARES, extra_sources: Sequence[str] = (), set_dir: Path | None = None) -> Row:
    """One line per (run, model) over all its repeats, one per repeat when there are several, and the union of
    the repeats (``unions``); ``extra_sources``: further source specs (e.g. cross-run unions). Every run must
    belong to the same set, or ``set_dir`` names the set to score on (:func:`scoring_context`)."""
    entries: list[tuple[str, Source]] = []
    all_runs: list[Row] = []
    warnings: list[str] = []
    for d in run_dirs:
        run, rows = load_run(d)
        all_runs.append(run)
        for m in run["models"]:
            mr = [r for r in rows if r["model"] == m]
            obs = [obs_of(r) for r in mr]
            label = f"{run_label(run, m)} [{d.name}]"
            warnings += [f"{label}: {w}" for w in stage_model_warnings(stage_models(run, m), observed_models(mr))]
            repeats = sorted({int(r["repeat"]) for r in mr})
            entries.append((label + (f" (r1-r{repeats[-1]} pooled)" if len(repeats) > 1 else ""),
                            Source(label, obs, [run])))
            if len(repeats) > 1:
                for k in repeats:
                    entries.append((f"{label} r{k}", Source(label, [o for o in obs if o.repeat == k], [run])))
                if unions:
                    entries.append((f"{label} union of {len(repeats)}", Source(label, union(obs), [run], True)))
    for spec in extra_sources:
        src = load_source(spec)
        all_runs += src.runs
        entries.append((src.label, src))
    ctx = scoring_context(all_runs, set_dir, gold_file, shares)
    scored = [(label, score_source(src.obs, ctx)) for label, src in entries]
    md = ["# technique_staged results", "",
          (f"Gold `{ctx.gold_label}`, full (candidate and dont_care rows excluded); slugs without group nodes; "
           f"tolerant = hierarchy-tolerant slugs, link credit {TOLERANT_PARTIAL} (D626); "
           f"delivered over all items; length shares `{shares}`; generated "
           f"{dt.datetime.now(dt.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}."), ""]
    md += _table(TABLE_HEAD, [table_row(label, s) for label, s in scored])
    md += ([""] + [f"**warning**: {w}" for w in warnings]) if warnings else []
    return {"sources": [{"label": label, "metrics": s} for label, s in scored], "length_shares": shares,
            "gold": ctx.gold_label, "tolerant_partial": TOLERANT_PARTIAL,
            "warnings": warnings, "markdown": "\n".join(md)}


def write_analysis(out_dir: Path, name: str, payload: Row) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"{stamp}-{name}.md"
    path.write_text(payload["markdown"] + "\n", "utf-8")
    path.with_suffix(".json").write_text(
        json.dumps({k: v for k, v in payload.items() if k != "markdown"}, indent=2, sort_keys=True, default=str)
        + "\n", "utf-8")
    return path


__all__ = ["BOOTSTRAP_RESAMPLES", "BOOTSTRAP_SEED", "DEFAULT_SHARES", "GOLD_VARIANTS", "LENGTH_BANDS", "LENGTH_SHARES",
           "PRIORITY_ROLES", "TOLERANT_PARTIAL", "AnalysisError", "Context", "Obs", "Source", "compare",
           "compare_markdown",
           "length_band", "load_context", "load_source", "obs_of", "results_table", "score_source", "scoring_context",
           "summarize", "taxonomy_structure", "tolerant_counts",
           "union", "write_analysis"]

