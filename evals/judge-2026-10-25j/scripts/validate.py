"""Validation pass over every judge output (no judge call): re-extract each final message from its ``.raw``, check it
against the task's ``schema.json``, apply the two deterministic rules, and write the validated labels.

    validate.py

Writes ``EVAL104_PRIVATE/validated/<judge>/<set>.jsonl`` (one row per (item, call): ``status`` valid / repaired /
over_length / invalid / missing, ``label`` (full text, never capped here), ``flags``, ``problems``, the run's
cost / tokens / turns / wall time / attempts / prompt sha256) and ``validated/summary.json``; prints the counts.
``score.py`` and ``release.py`` read these files.

Rules (coordinator, 2026-10-26):

* **Structural misnesting with all content present** -> repaired deterministically, ``flags.repaired = true``. The one
  repair implemented: the message parses as one object followed only by closing braces (``Extra data``), and moving
  each top-level key the schema does not allow at the top level into the one nested object whose schema allows it
  and lacks it makes the label valid (seen: Opus closed ``sr`` before ``category`` / ``agenda``). Nothing is dropped or
  invented; the moved keys are listed in ``flags.moved``.
* **Length-cap overruns** (``maxLength`` errors only) -> accepted, ``flags.over_length = true`` with the paths.
* Anything else -> ``invalid``, listed with its problems; no guessing.

The runner's own verdict is cross-checked: a record the runner marked ``ok`` must re-validate to the same label.
"""
from __future__ import annotations

import collections
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import run_judges as R  # noqa: E402


def final_message(judge: str, raw: str) -> str | None:
    return (R.parse_claude(raw) if judge in R.CLAUDE_JUDGES else R.parse_codex(raw))["message"]


def schema_errors(obj: Any, schema: dict) -> list[Any]:
    import jsonschema

    return list(jsonschema.Draft202012Validator(schema).iter_errors(obj))


def try_misnesting(msg: str, schema: dict) -> tuple[dict | None, list[str]]:
    """The repair of the module docstring, or (None, [])."""
    t = msg.strip()
    try:
        obj, end = json.JSONDecoder().raw_decode(t)
    except json.JSONDecodeError:
        return None, []
    rest = t[end:].strip()
    if not isinstance(obj, dict) or not rest or set(rest) - {"}"}:
        return None, []
    top = schema.get("properties", {})
    moved = []
    for k in [k for k in obj if k not in top]:
        homes = [h for h, hs in top.items() if isinstance(obj.get(h), dict)
                 and k in (hs.get("properties") or {}) and k not in obj[h]]
        if len(homes) != 1:
            return None, []
        obj[homes[0]][k] = obj.pop(k)
        moved.append(f"{k}->{homes[0]}")
    if not moved or schema_errors(obj, schema):
        return None, []
    return obj, moved


def validate_one(judge: str, set_name: str, item: str, call: str) -> dict[str, Any]:
    raw_p, json_p = C.run_paths(judge, set_name, item, call)
    base = {"item_id": item, "call": call, "judge": judge}
    if not json_p.exists() or not raw_p.exists():
        return {**base, "status": "missing", "label": None, "flags": {}, "problems": ["no run record"]}
    rec = json.loads(json_p.read_text("utf-8"))
    meta = {k: rec.get(k) for k in ("cost_usd", "tokens", "turns", "wall_s", "attempts", "prompt_sha256", "started",
                                    "finished")}
    meta["history_cost_usd"] = sum(h.get("cost_usd") or 0 for h in rec.get("history") or [])
    schema = json.loads((C.task_dir(set_name, item, call) / "schema.json").read_text("utf-8"))
    msg = final_message(judge, raw_p.read_text("utf-8"))
    label, note = R.extract_json(msg)
    flags: dict[str, Any] = {}
    problems: list[str] = []
    status = "valid"
    if label is None:
        fixed, moved = try_misnesting(msg or "", schema)
        if fixed is not None:
            label, status, flags = fixed, "repaired", {"repaired": True, "moved": moved}
        else:
            return {**base, "status": "invalid", "label": None, "flags": {}, "problems": [note or "no label"],
                    "meta": meta, "runner_ok": rec.get("ok")}
    else:
        if note:
            flags["extracted_from_text"] = True
        errs = schema_errors(label, schema)
        hard = [e for e in errs if e.validator != "maxLength"]
        if hard:
            status = "invalid"
            problems = [f"{'/'.join(map(str, e.absolute_path))}: {e.message[:200]}" for e in hard[:10]]
        elif errs:
            status = "over_length"
            flags["over_length"] = True
            flags["over_length_paths"] = ["/".join(map(str, e.absolute_path)) for e in errs]
    if rec.get("ok") and (status != "valid" or label != rec.get("label")):
        problems.append("runner marked ok but the re-validation differs")
    return {**base, "status": status, "label": label if status != "invalid" else None, "flags": flags,
            "problems": problems, "meta": meta, "runner_ok": rec.get("ok")}


def main() -> int:
    out = C.private_root() / "validated"
    summary: dict[str, Any] = {}
    for judge in C.JUDGES:
        for set_name in C.SETS:
            items = [r["item_id"] for r in C.read_jsonl(C.SET_ROOT / set_name / "items.jsonl")]
            rows = [validate_one(judge, set_name, i, c) for i in items for c in C.CALLS[set_name]]
            C.write_jsonl(out / judge / f"{set_name}.jsonl", rows)
            for c in C.CALLS[set_name]:
                cnt = collections.Counter(r["status"] for r in rows if r["call"] == c)
                summary[f"{judge}/{set_name}/{c}"] = {
                    **dict(cnt), "listed": [{"item_id": r["item_id"], "status": r["status"], "flags": r["flags"],
                                             "problems": r["problems"]} for r in rows
                                            if r["call"] == c and (r["status"] != "valid" or r["problems"])]}
    (out / "summary.json").write_text(C.dumps(summary), "utf-8")
    for k, v in summary.items():
        print(k, {s: n for s, n in v.items() if s != "listed"}, *(json.dumps(x) for x in v["listed"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
