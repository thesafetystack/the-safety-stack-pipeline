#!/usr/bin/env python3
"""``tools/list_configs.py`` — the config digests a stage recorded in a store, for writing a plan line's ``target``
and ``accepted:`` list (D758, :mod:`stack.plan` "Accepted configs"; ``docs/OPERATE.md`` "Keeping answers across a
config change").

For ``annotate.<wf>``: one line per config digest that a run record of the stage carries (its ``prompt_pin`` hashes
to it): its name as an ``accepted:`` entry must give it (:func:`stack.annotate.stage.config_name`), models, registry
version, produced records, the runs (count, first and last start), whether the plan's current pin is that config
(``CURRENT``; ``ACCEPTED`` for one the line accepts), and the rows of the build's ``<wf>`` file under it; then the
plan line's selection digest (:func:`stack.plan.selection_digest` over the current config and the line's accepted
list). For ``triage``: one line per (question, question digest, prompt name) of the build's ``triage_evidence``
file with its rows by status, the plan's current question digests and the line's selection digest. ``--build`` is a build id or ``latest`` (default: the newest ``ok`` or ``incomplete``
build of ``--plan``).

    uv run --no-sync python tools/list_configs.py --store <store> --plan batch-2026-10a annotate.technique_staged
    uv run --no-sync python tools/list_configs.py --store <store> --plan batch-2026-10a triage

Read-only: reads the store's run and build records, the build's files and the plan; writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from stack import duck, gate
from stack.annotate.stage import PIN_DIGEST_KEYS, config_digest, config_name, resolve_pin, stack_config
from stack.build import find_plan
from stack.checks.digests import H
from stack.fold.release import resolve_build
from stack.plan import load_plan
from stack.stage_runner import load_stage_module
from stack.store.index import store_index


def current_annotate(plan: Any, stage: str) -> str:
    wf = load_stage_module(stage).WORKFLOW
    pin = dict(plan.prompts.get(stage, {}))
    return config_digest(wf.spec, pin, resolve_pin(wf, pin, stack_config))


def annotate_lines(root: Path, stage: str, build: dict[str, Any], current: str, accepted: tuple[str, ...]) -> list[str]:
    configs: dict[str, dict[str, Any]] = {}
    for rec in store_index(root).records():
        p = rec.prompt_pin
        if rec.stage != stage or not p or not all(k in p for k in PIN_DIGEST_KEYS):
            continue
        c = configs.setdefault(H({k: p[k] for k in PIN_DIGEST_KEYS}), {"pin": p, "runs": []})
        c["runs"].append(rec.started_at)
    wf = stage.removeprefix("annotate.")
    rows: Counter[str] = Counter()
    f = (build.get("files") or {}).get(wf)
    if f is not None:
        con = duck.connect()
        for d, n in con.execute("SELECT config_digest, count(DISTINCT doc_id) FROM read_parquet(?) GROUP BY 1",
                                [str(root / f["path"])]).fetchall():
            rows[d] = n
    out = []
    def order(x: str) -> tuple[bool, str]:  # recorded configs by their first run, then override digests
        return x not in configs, min(configs[x]["runs"]).isoformat() if x in configs else x

    for d in sorted(set(configs) | set(rows), key=order):
        c = configs.get(d)
        if c is None:
            out.append(f"{d}  rows {rows[d]:>7}  (no run record pins it: an override-supplied row's digest)")
            continue
        p, runs = c["pin"], sorted(c["runs"])
        mark = "CURRENT " if d == current else "ACCEPTED " if d in accepted else ""
        out.append(f"{d}  rows {rows.get(d, 0):>7}  {mark}name {config_name(p)!r} "
                   f"models {p.get('models')} registry_version {p.get('registry_version')} produced "
                   f"{p.get('produced')} runs {len(runs)} ({runs[0]:%Y-%m-%d} .. {runs[-1]:%Y-%m-%d})")
    return out


def triage_lines(root: Path, build: dict[str, Any], plan: Any) -> list[str]:
    cur = {q.name: q.config_digest for q in gate.questions(plan)}
    f = (build.get("files") or {}).get("triage_evidence")
    out = [f"current: {name} {d}" for name, d in cur.items()]
    if f is None:
        return [*out, "(the build has no triage_evidence file)"]
    con = duck.connect()
    for q, d, name, st, n in con.execute("SELECT question, config_digest, prompt_name, status, count(*) "
                                         "FROM read_parquet(?) GROUP BY ALL ORDER BY 1, 2, 3, 4",
                                         [str(root / f["path"])]).fetchall():
        out.append(f"{q} {d} {name}  {st} {n}{'  CURRENT' if cur.get(q) == d else ''}")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", help="annotate.<wf> or triage")
    ap.add_argument("--store", required=True, type=Path)
    ap.add_argument("--plan", required=True, help="the plan (name or file) whose current configs are marked")
    ap.add_argument("--build", default="latest")
    a = ap.parse_args(argv)
    plan = load_plan(find_plan(a.plan))
    build = resolve_build(a.store, a.build, plan.plan)
    print(f"build {build.get('build_id')} ({build.get('status')}), plan {plan.plan} {plan.plan_digest[:12]}…")
    line = plan.stage_line(a.stage)
    if a.stage == "triage":
        lines = triage_lines(a.store, build, plan)
        if line is not None:
            sel = line.selection({q.name: q.config_digest for q in gate.questions(plan)})
            lines.append(f"selection digest {sel['digest']} (accepted: {len(sel['accepted'])})")
    elif a.stage.startswith("annotate."):
        cur = current_annotate(plan, a.stage)
        lines = annotate_lines(a.store, a.stage, build, cur, line.accepted_configs() if line is not None else ())
        if line is not None:
            sel = line.selection(cur)
            lines.append(f"selection digest {sel['digest']} (target {cur[:12]}…, accepted: {len(sel['accepted'])})")
    else:
        print(f"{a.stage}: annotate.<wf> or triage", file=sys.stderr)
        return 2
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
