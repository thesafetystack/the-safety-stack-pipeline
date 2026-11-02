"""Assemble the releasable files of each set from the validated judge labels and the scores (no judge call).

    release.py [--sets ...]

Per set directory (here): ``labels-opus.jsonl``, ``labels-sol.jsonl``, ``labels-sonnet.jsonl``, ``scores.md``,
``scores.json``, ``disagreements.jsonl`` (``production.jsonl`` is the freeze's, capped the same way). Reads
``EVAL104_PRIVATE/validated/<judge>/<set>.jsonl`` (validate.py) and ``EVAL104_PRIVATE/scores/<set>.{md,json}``
(score.py). Per label row: item and document ids, call, judge model and CLI with version, validation status and
flags, the label, cost (claude's notional ``total_cost_usd``, retries included) or tokens (codex), turns, wall time,
attempts, and the sha256 of the wrapper prompt, guide and schema the judge saw.

Capping (the dataset release's rule, ``stack.fold.dataset.cut_string``): every verbatim field (``common.VERBATIM_KEYS``)
at 300 characters at a word boundary with "…"; a field over its schema ``maxLength`` (validation flag
``over_length``; the full text is kept in the private validated file) at that length; a reasoning field
(``REASONING_KEYS``) is left whole unless it carries more than 300 characters of contiguous document text (compared
whitespace-collapsed), then it is cut at 300 and flagged ``long_document_text``. Every cut is listed in
``flags.capped``.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
from run_judges import CLAUDE_JUDGES  # noqa: E402

CAP = 300
REASONING_KEYS = frozenset({"reasoning", "rationale", "note", "notes", "reason", "why", "gloss", "doc_note",
                            "off_topic_reason", "what", "where"})
CLI = {"opus": {"model": "claude-opus-5-5", "cli": "claude", "cli_version": "2.1.308 (Claude Code)",
                "effort": "high"},
       "sol": {"model": "gpt-6.1-sol", "cli": "codex exec", "cli_version": "codex-cli 0.162.1",
               "effort": "model_reasoning_effort=high"},
       "sonnet": {"model": "claude-sonnet-5-5", "cli": "claude", "cli_version": "2.1.308 (Claude Code)",
                  "effort": "high"}}
_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub(" ", s).strip()


def has_long_doc_text(s: str, doc: str) -> bool:
    n = norm(s)
    if len(n) <= CAP:
        return False
    return any(n[k:k + CAP + 1] in doc for k in range(0, len(n) - CAP))


def walk_cap(obj: Any, doc: str, path: str, capped: list[str], over: set[str], schema_max: dict[str, int],
             in_verbatim: bool = False) -> Any:
    from stack.fold.dataset import cut_string

    if isinstance(obj, dict):
        return {k: walk_cap(v, doc, f"{path}/{k}" if path else k, capped, over, schema_max, k in C.VERBATIM_KEYS)
                for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk_cap(v, doc, f"{path}/{i}", capped, over, schema_max, in_verbatim) for i, v in enumerate(obj)]
    if not isinstance(obj, str):
        return obj
    key = path.rsplit("/", 1)[-1]
    if path in over and path in schema_max and len(obj) > schema_max[path]:
        capped.append(f"{path}:over_length->{schema_max[path]}")
        return cut_string(obj, schema_max[path])
    if in_verbatim and len(obj) > CAP:
        capped.append(f"{path}:verbatim->{CAP}")
        return cut_string(obj, CAP)
    if key in REASONING_KEYS and has_long_doc_text(obj, doc):
        capped.append(f"{path}:long_document_text->{CAP}")
        return cut_string(obj, CAP)
    return obj


def max_lengths(schema: dict, paths: list[str]) -> dict[str, int]:
    """The schema ``maxLength`` of each flagged path (object keys only; array indices skipped)."""
    out = {}
    for p in paths:
        node = schema
        for part in p.split("/"):
            if part.isdigit():
                node = node.get("items", {})
            else:
                node = (node.get("properties") or {}).get(part, {})
            node = next((a for a in node.get("anyOf", []) if a.get("type") != "null"), node) if "anyOf" in node else node
        if "maxLength" in node:
            out[p] = node["maxLength"]
    return out


def doc_text(set_name: str, item: dict, call: str) -> str:
    d = C.task_dir(set_name, item["item_id"], call)
    f = d / ("record.txt" if call == "triage" else "text.txt")
    return norm(f.read_text("utf-8"))


def release_set(set_name: str) -> dict[str, Any]:
    items = {r["item_id"]: r for r in C.read_jsonl(C.SET_ROOT / set_name / "items.jsonl")}
    stats: dict[str, Any] = {}
    for judge in C.JUDGES:
        rows = []
        n_capped = n_long = 0
        for v in C.read_jsonl(C.private_root() / "validated" / judge / f"{set_name}.jsonl"):
            it = items[v["item_id"]]
            call = v["call"]
            label = v["label"]
            capped: list[str] = []
            if label is not None:
                schema = json.loads((C.task_dir(set_name, v["item_id"], call) / "schema.json").read_text("utf-8"))
                over = set(v["flags"].get("over_length_paths") or [])
                label = walk_cap(label, doc_text(set_name, it, call), "", capped, over, max_lengths(schema, list(over)))
            flags = {k: x for k, x in v["flags"].items() if k != "over_length_paths"}
            if capped:
                flags["capped"] = capped
                n_capped += 1
            if any(":long_document_text" in c for c in capped):
                flags["long_document_text"] = True
                n_long += 1
            meta = v.get("meta") or {}
            tf = it["task_files"][call]
            rows.append({"item_id": v["item_id"], "doc_id": it["doc_id"], "call": call, "judge": judge, **CLI[judge],
                         "status": v["status"], "flags": flags, "problems": v.get("problems") or [], "label": label,
                         "cost_usd": (meta.get("cost_usd") or 0) + (meta.get("history_cost_usd") or 0)
                         if judge in CLAUDE_JUDGES else None,
                         "tokens": meta.get("tokens"), "turns": meta.get("turns"), "wall_s": meta.get("wall_s"),
                         "attempts": meta.get("attempts"), "prompt_sha256": meta.get("prompt_sha256"),
                         "guide_sha256": tf.get("guide.md"), "schema_sha256": tf.get("schema.json")})
        C.write_jsonl(C.SET_ROOT / set_name / f"labels-{judge}.jsonl", rows)
        stats[judge] = {"rows": len(rows), "rows_with_cuts": n_capped, "long_document_text": n_long}
    sc = C.private_root() / "scores"
    shutil.copyfile(sc / f"{set_name}.md", C.SET_ROOT / set_name / "scores.md")
    js = json.loads((sc / f"{set_name}.json").read_text("utf-8"))
    dis = js.pop("disagreements")
    (C.SET_ROOT / set_name / "scores.json").write_text(C.dumps(js), "utf-8")
    C.write_jsonl(C.SET_ROOT / set_name / "disagreements.jsonl", [C.cap_verbatim(d, CAP) for d in dis])
    prod = C.read_jsonl(C.SET_ROOT / set_name / "production.jsonl")
    stats["production_long_document_text"] = sum(
        has_long_doc_text(str(r["label"].get(k) or ""), doc_text(set_name, items[r["item_id"]], r["call"]))
        for r in prod for k in ("reasoning", "rationale") if isinstance(r["label"].get(k), str))
    stats["disagreements"] = len(dis)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sets", default=",".join(C.SETS))
    a = ap.parse_args()
    for s in a.sets.split(","):
        print(s, json.dumps(release_set(s)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
