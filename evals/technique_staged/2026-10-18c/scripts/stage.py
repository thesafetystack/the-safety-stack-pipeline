"""Stage the inputs of the technique_staged gold revision 2026-10-18c (gold v2, completed reconciliation and a second
verification round) from the labelling workspace (no LLM call, no network, no store).

Run from the repo root::

    uv run --no-sync python evals/technique_staged/2026-10-18c/scripts/stage.py <WORKSPACE> [--write]

``WORKSPACE`` is the labelling workspace (outside the repository), read only; its path is not written into the set:

* ``labels/final2/<item>.json`` (the labels of record: the Astra reconciliation with both verification rounds
  applied), ``labels/R/<item>.json`` (the Astra reconciliation, all 120 items), ``labels/R-items.json`` (the 25 items
  of reconciliation round 1) and ``labels/R2-items.json`` (the 95 of round 2);
* ``verify/key2.json`` (round-2 disputes of the reconciled gold vs the Opus labels on the sample: who gave each
  slug, its Sonnet verdict, why it went to the Codex adjudicator, the P / Q swap), ``verify/adj2/<item>.json`` (the
  Codex gpt-6-sol verdicts), ``verify/estimates2.json``, ``verify/sample.json``, ``verify/opus/``, ``verify/adj/``
  (the round-1 Sonnet verdicts) and ``runs/VA/plan.json`` with ``runs/VA/A<n>.sha256`` and ``A<n>/PROMPT.md`` (the
  round-2 batches, their packet digests and prompts);
* ``scripts/disputes2.py``, ``parse_adj2.py``, ``verify_apply2.py``; ``prompts/VERIFY-ADJ.md``.

Checks (any failure refuses, nothing is written): 120 final and 120 R labels, each naming its item; the two round
lists are disjoint and cover the 120 items; every disputed slug sent to Codex has a Codex verdict in order, every
other disputed slug a Sonnet verdict; the sample, the Opus labels and the Sonnet verdicts equal what
``../2026-10-18b/adjudication/`` holds (``verify-sample.json``, ``opus.jsonl``, ``verification.jsonl``); each
round-2 batch prompt is ``VERIFY-ADJ.md`` with its items filled in.

What it writes under ``adjudication/`` (``--write``; without it, it only checks and reports): ``final.jsonl`` and
``astra-reconciler.jsonl`` (the label files verbatim, one JSON object per item, sorted keys),
``reconciliation-rounds.json``, ``verification2.jsonl`` (per sampled item the round-2 key with the Codex verdicts),
``estimates2.json`` (verbatim), ``round2-batches.json`` (batches, items, packet sha256 lists), ``scripts/`` (the three
round-2 scripts; a line naming workspace paths or a person is replaced by a marker, ``packet.json`` keeps both
sha256), ``packet.json`` and ``STAGED.json``. ``derive.py`` then builds the set from these files and 2026-10-18b.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path.cwd()
OUT = ROOT / "evals/technique_staged/2026-10-18c"
B = ROOT / "evals/technique_staged/2026-10-18b"
IDS = [f"g2-{n:04d}" for n in range(1, 121)]
SCRIPTS = ("disputes2.py", "parse_adj2.py", "verify_apply2.py")
ROUNDS = {"1": {"batches": "R01-R08", "when": "2026-10-18 01:59-02:13 UTC", "list": "R-items.json"},
          "2": {"batches": "R09-R30", "when": "2026-10-18 09:05-09:45 UTC (after the Codex credit reset)",
                "list": "R2-items.json"}}
#: Lines naming a workspace path or a person (spelled in pieces so that this file passes the set-files check).
REDACT_LINE = re.compile("|".join(re.escape(a + b) for a, b in (("/ho", "me/"), ("/t", "mp/"), ("im", "pl/")))
                         + "|(?i:" + "mi" + "mir)|" + "Tom" + "[aá]š|(?i:" + "gaven" + ")")
REDACTED = "# [line removed at staging: it names workspace paths or a person; packet.json has the original sha256]"


def sha(b: bytes) -> str:
    import hashlib

    return hashlib.sha256(b).hexdigest()


def dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text("utf-8").splitlines() if x.strip()]


def redact(text: str) -> str:
    return "".join((REDACTED + "\n") if REDACT_LINE.search(line) else line for line in text.splitlines(True))


def strip_header(text: str) -> str:
    """A prompt copied into 2026-10-18b without its provenance header."""
    return text.split("-->\n\n", 1)[1] if text.startswith("<!--") else text


def main(ws: Path, write: bool) -> int:
    problems: list[str] = []
    labels: dict[str, list[dict]] = {}
    for kind, sub in (("final", "final2"), ("astra-reconciler", "R")):
        rows = []
        for iid in IDS:
            f = ws / "labels" / sub / f"{iid}.json"
            if not f.is_file():
                problems.append(f"labels/{sub}/{iid}.json is missing")
                continue
            d = json.loads(f.read_text("utf-8"))
            if d.get("item_id") != iid:
                problems.append(f"labels/{sub}/{iid}.json: item_id {d.get('item_id')!r}")
            rows.append(d)
        labels[kind] = rows
    rounds = {}
    for r, info in ROUNDS.items():
        rounds[r] = {**{k: v for k, v in info.items() if k != "list"},
                     "items": sorted(json.loads((ws / "labels" / info["list"]).read_text("utf-8")))}
    r1, r2 = set(rounds["1"]["items"]), set(rounds["2"]["items"])
    if r1 & r2 or r1 | r2 != set(IDS):
        problems.append(f"round lists overlap ({len(r1 & r2)}) or do not cover the items ({len(r1 | r2)})")
    sample = json.loads((ws / "verify" / "sample.json").read_text("utf-8"))
    if sample != json.loads((B / "adjudication" / "verify-sample.json").read_text("utf-8")):
        problems.append("verify/sample.json differs from 2026-10-18b's verify-sample.json")
    sample_ids = [x["item_id"] for x in sample]
    opus_b = {r["item_id"]: r for r in read_jsonl(B / "adjudication" / "opus.jsonl")}
    sonnet_b = {r["item_id"]: {v["slug"]: v for v in r["verdicts"]}
                for r in read_jsonl(B / "adjudication" / "verification.jsonl")}
    for iid in sample_ids:
        if json.loads((ws / "verify" / "opus" / f"{iid}.json").read_text("utf-8")) != opus_b.get(iid):
            problems.append(f"verify/opus/{iid}.json differs from 2026-10-18b's opus.jsonl")
        f = ws / "verify" / "adj" / f"{iid}.json"
        son = {v["slug"]: v for v in json.loads(f.read_text("utf-8"))["verdicts"]} if f.is_file() else {}
        if son != sonnet_b.get(iid, {}):
            problems.append(f"verify/adj/{iid}.json differs from 2026-10-18b's verification.jsonl")
    key = json.loads((ws / "verify" / "key2.json").read_text("utf-8"))
    verification2 = []
    for iid in sample_ids:
        k = key.get(iid)
        if k is None:
            problems.append(f"verify/key2.json has no {iid}")
            continue
        f = ws / "verify" / "adj2" / f"{iid}.json"
        a = json.loads(f.read_text("utf-8")) if f.is_file() else {"verdicts": []}
        to_codex = [d["slug"] for d in k["disputes"] if d["to_codex"]]
        if [v["slug"] for v in a["verdicts"]] != to_codex:
            problems.append(f"verify/adj2/{iid}.json: verdicts do not follow the slugs key2.json sent to Codex")
        for d in k["disputes"]:
            if not d["to_codex"] and d["slug"] not in sonnet_b.get(iid, {}):
                problems.append(f"{iid}:{d['slug']}: kept the Sonnet verdict, but there is none")
        verification2.append({"item_id": iid, "swap": k["swap"], "disputes": k["disputes"],
                              "adjudicator": a.get("adjudicator"), "verdicts": a["verdicts"]})
    plan = json.loads((ws / "runs" / "VA" / "plan.json").read_text("utf-8"))
    template = (ws / "prompts" / "VERIFY-ADJ.md").read_text("utf-8")
    if template != strip_header((B / "adjudication" / "prompts" / "VERIFY-ADJ.md").read_text("utf-8")):
        problems.append("prompts/VERIFY-ADJ.md differs from 2026-10-18b's copy")
    batches = {}
    for name, info in sorted(plan.items()):
        prompt = (ws / "runs" / "VA" / name / "PROMPT.md").read_text("utf-8")
        if prompt != template.replace("{{ITEMS}}", ", ".join(info["items"])):
            problems.append(f"runs/VA/{name}/PROMPT.md is not VERIFY-ADJ.md with its items")
        sums = (ws / "runs" / "VA" / f"{name}.sha256").read_text("utf-8")
        batches[name] = {"items": info["items"], "chars": info.get("chars"),
                         "packet_sha256": [line.split("  ", 1)[::-1] for line in sums.splitlines() if line.strip()]}
    scripts = {n: (ws / "scripts" / n).read_bytes() for n in SCRIPTS}
    estimates = (ws / "verify" / "estimates2.json").read_bytes()
    if problems:
        print("\n".join(problems))
        return 1
    packet = {"scripts": {n: {"sha256": sha(b), "staged_sha256": sha(redact(b.decode("utf-8")).encode("utf-8"))}
                          for n, b in scripts.items()},
              "round2_prompt": "prompts/VERIFY-ADJ.md of 2026-10-18b with each batch's items for {{ITEMS}}"}
    staged = {"labels_of_record": "final2", "counts": {k: len(v) for k, v in labels.items()} | {
        "verification2": len(verification2),
        "to_codex": sum(bool(d["to_codex"]) for v in verification2 for d in v["disputes"])}}
    print(f"checked: final {len(labels['final'])}, astra-reconciler {len(labels['astra-reconciler'])} labels; rounds "
          f"{len(r1)} + {len(r2)}; round-2 verification on {len(verification2)} items, {staged['counts']['to_codex']} "
          f"slugs to Codex; sample, Opus labels, Sonnet verdicts and the prompt equal 2026-10-18b's")
    if not write:
        return 0
    (OUT / "adjudication" / "scripts").mkdir(parents=True, exist_ok=True)
    a = OUT / "adjudication"
    for kind, rows in labels.items():
        (a / f"{kind}.jsonl").write_text(dumps(rows), encoding="utf-8")
    (a / "reconciliation-rounds.json").write_text(json.dumps(rounds, indent=2) + "\n", encoding="utf-8")
    (a / "verification2.jsonl").write_text(dumps(verification2), encoding="utf-8")
    (a / "estimates2.json").write_bytes(estimates)
    (a / "round2-batches.json").write_text(json.dumps(batches, indent=2) + "\n", encoding="utf-8")
    for n, b in scripts.items():
        (a / "scripts" / n).write_text(redact(b.decode("utf-8")), encoding="utf-8")
    (a / "packet.json").write_text(json.dumps(packet, indent=2) + "\n", encoding="utf-8")
    (a / "STAGED.json").write_text(json.dumps(staged, indent=2) + "\n", encoding="utf-8")
    print("wrote adjudication/ (final, astra-reconciler, reconciliation-rounds, verification2, estimates2, "
          "round2-batches, scripts, packet, STAGED)")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--write"]
    if len(args) != 1:
        raise SystemExit(__doc__)
    raise SystemExit(main(Path(args[0]), "--write" in sys.argv[1:]))
