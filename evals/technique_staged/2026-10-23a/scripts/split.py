"""The dev / test split of the gold v3 sample (analysis only; no LLM call, no store access).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-23a/scripts/split.py

Stratified by source kind x length band (``sources/sample.jsonl`` ``source_kind`` and ``band``): each cell's items
in a seeded random order, the first ``round_half_down(DEV_SHARE * n)`` go to dev, and the cells' remainders are
filled by largest remainder (ties by cell name) until dev has exactly ``DEV_N`` items. Writes ``split.json`` into
this set and into ``evals/model_roles/2026-10-23a/`` (the two sets share their items), byte-identical. Prompt and
configuration tuning (error samples) reads dev only; every arm is reported on dev, test and all.

This split is not the ``split`` field of ``items.jsonl`` (the freeze's default assignment, left unchanged so the
frozen sets' digests hold).
"""
from __future__ import annotations

import json
import math
import random
from collections import defaultdict
from pathlib import Path

SET = Path(__file__).resolve().parents[1]
REPO = SET.parents[2]
SEED = 20261024
DEV_N = 60
DEV_SHARE = DEV_N / 100


def split(sample: list[dict]) -> dict:
    cells: dict[str, list[str]] = defaultdict(list)
    for r in sample:
        cells[f"{r['source_kind']} x {r['band']}"].append(r["item_id"])
    rng = random.Random(SEED)
    order = {c: rng.sample(sorted(v), len(v)) for c, v in sorted(cells.items())}
    take = {c: math.floor(DEV_SHARE * len(v)) for c, v in order.items()}
    left = DEV_N - sum(take.values())
    by_rem = sorted(order, key=lambda c: (-(DEV_SHARE * len(order[c]) - take[c]), c))
    for c in by_rem[:left]:
        take[c] += 1
    dev = sorted(i for c, v in order.items() for i in v[:take[c]])
    test = sorted(i for c, v in order.items() for i in v[take[c]:])
    return {"seed": SEED, "method": "stratified by source_kind x band, largest remainder to 60 dev (scripts/split.py)",
            "dev": dev, "test": test,
            "cells": {c: {"n": len(v), "dev": take[c], "test": len(v) - take[c]} for c, v in order.items()}}


def main() -> None:
    sample = [json.loads(x) for x in (SET / "sources" / "sample.jsonl").read_text().splitlines() if x.strip()]
    out = json.dumps(split(sample), indent=1, sort_keys=True) + "\n"
    for d in (SET, REPO / "evals" / "model_roles" / "2026-10-23a"):
        (d / "split.json").write_text(out)
    s = json.loads(out)
    print(f"dev {len(s['dev'])}, test {len(s['test'])}")
    for c, v in s["cells"].items():
        print(f"  {c}: n {v['n']}, dev {v['dev']}, test {v['test']}")


if __name__ == "__main__":
    main()
