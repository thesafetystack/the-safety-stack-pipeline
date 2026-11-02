"""Extract, once, the roles the two audited arms gave to their audited false positives (no LLM call, no network).

Run from the repo root:
``uv run --no-sync python evals/technique_staged/2026-10-17b/scripts/arm_roles.py STAGED_RUN_DIR SINGLE_RUN_DIR [--write]``

The blind audit (``adjudication/audit/``) sampled pool ``B-staged`` from the false positives of staged gpt-6-luna
(prompt set technique-staged-v1, run ``20261017T173436Z_all_31133ca9``, 2 repeats) and pool ``B-single`` from those
of single gpt-6-luna (``20261017T165200Z_all_5dd45f5f``, 3 repeats), both made on 2026-10-17a. The run directories
live in a local eval store (gitignored), so ``scripts/derive.py`` cannot read them; this script copies what it
needs into ``adjudication/audit/arm-roles.jsonl``: per pool-B audit item, the run id and, per repeat whose row
names the slug, that row's nonzero roles. The run directories are arguments; they are not recorded.

Refuses when a run is not the expected one (run id, mode, prompt set, set items digest). Without ``--write`` it
compares with the file byte for byte (exit 1 on a difference).
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path.cwd()
SET = ROOT / "evals/technique_staged/2026-10-17b"
AUDIT = SET / "adjudication" / "audit"
OUT = AUDIT / "arm-roles.jsonl"
#: pool -> (run id, mode, prompt set)
ARMS = {"B-staged": ("eval-technique_staged-20261017T173436Z_all_31133ca9", "staged", "technique-staged-v1"),
        "B-single": ("eval-technique_staged-20261017T165200Z_all_5dd45f5f", "single", "technique-staged-v1")}


class ExtractError(Exception):
    """A run that is not the audited arm, or an audit item the arm never predicted: nothing is written."""


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def load_arm(pool: str, run_dir: Path) -> list[dict]:
    run = json.loads((run_dir / "run.json").read_text("utf-8"))
    want_id, mode, prompt = ARMS[pool]
    items = hashlib.sha256((SET / "items.jsonl").read_bytes()).hexdigest()
    got = (run.get("run_id"), run.get("mode"), run.get("prompt"), run.get("set_items_digest"))
    if got != (want_id, mode, prompt, items):
        raise ExtractError(f"{pool}: run {got[:3]} (items {str(got[3])[:12]}) is not the audited arm "
                           f"{(want_id, mode, prompt)} on items {items[:12]}")
    return read_jsonl(run_dir / "rows.jsonl")


def build(staged: Path, single: Path) -> str:
    rows = {"B-staged": load_arm("B-staged", staged), "B-single": load_arm("B-single", single)}
    out = []
    for k in sorted(read_jsonl(AUDIT / "key.jsonl"), key=lambda x: x["id"]):
        if k["pool"] not in ARMS:
            continue
        reps = []
        for r in sorted(rows[k["pool"]], key=lambda x: int(x["repeat"])):
            if r["item_id"] != k["item"] or r["status"] != "ok":
                continue
            for t in (r.get("payload") or {}).get("techniques") or []:
                if t["slug"] == k["slug"]:
                    reps.append({"repeat": int(r["repeat"]),
                                 "roles": {role: int(v) for role, v in sorted((t.get("roles") or {}).items()) if v}})
        if not reps:
            raise ExtractError(f"{k['id']}: {k['pool']} arm has no row naming {k['slug']} on {k['item']}")
        out.append({"id": k["id"], "pool": k["pool"], "item": k["item"], "slug": k["slug"],
                    "run_id": ARMS[k["pool"]][0], "repeats": reps})
    return "".join(json.dumps(x, ensure_ascii=False, sort_keys=True) + "\n" for x in out)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 2:
        print(__doc__)
        return 2
    try:
        text = build(Path(args[0]), Path(args[1]))
    except ExtractError as exc:
        print(f"refused: {exc}")
        return 1
    if "--write" in argv:
        OUT.write_text(text, encoding="utf-8")
        print(f"wrote {OUT.relative_to(ROOT)} ({text.count(chr(10))} items)")
        return 0
    if not OUT.is_file() or OUT.read_text("utf-8") != text:
        print(f"differs: {OUT.relative_to(ROOT)}")
        return 1
    print(f"reproduced: {OUT.relative_to(ROOT)} byte for byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
