"""Check adjudication answers (stdlib only). Usage, from the session directory:
    python3 check.py out                 # every out/<item_id>.json against docs/<item_id>/candidates.json
    python3 check.py answer.json         # a combined {"documents": [...]} answer
Prints one line per problem and a final OK / FAIL line; exit status 1 on any problem.
"""
import json
import re
import sys
from pathlib import Path

VERDICTS = {"present", "absent", "borderline"}
SEVERITIES = {"core", "substantive", "peripheral", None}
RULES = {f"R{i}" for i in range(1, 11)}


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


def check_doc(here: Path, doc: dict, problems: list) -> None:
    iid = doc.get("item_id")
    cf = here / "docs" / str(iid) / "candidates.json"
    if not cf.exists():
        problems.append(f"{iid}: unknown item_id")
        return
    want = [c["slug"] for c in json.loads(cf.read_text())["candidates"]]
    text = norm((here / "docs" / str(iid) / "text.txt").read_text(errors="replace"))
    got = [x.get("slug") for x in doc.get("decisions") or []]
    if sorted(got) != sorted(want):
        problems.append(f"{iid}: decisions slugs {sorted(set(got) ^ set(want))} differ from candidates "
                        f"(or duplicates)")
    if not doc.get("adjudicator"):
        problems.append(f"{iid}: adjudicator missing")
    for x in doc.get("decisions") or []:
        s = x.get("slug")
        v = x.get("verdict")
        if v not in VERDICTS:
            problems.append(f"{iid}/{s}: verdict {v!r}")
        if x.get("severity") not in SEVERITIES:
            problems.append(f"{iid}/{s}: severity {x.get('severity')!r}")
        if v in ("present", "borderline") and x.get("severity") is None:
            problems.append(f"{iid}/{s}: severity required for {v}")
        if x.get("rule") not in RULES:
            problems.append(f"{iid}/{s}: rule {x.get('rule')!r}")
        q = x.get("quote") or ""
        if len(q) > 200:
            problems.append(f"{iid}/{s}: quote over 200 characters")
        if v == "present" and not q:
            problems.append(f"{iid}/{s}: present needs a quote")
        if q:
            parts = [p for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if p.strip()]
            if not all(norm(p) in text for p in parts):
                problems.append(f"{iid}/{s}: quote not found verbatim in text.txt")


def main() -> None:
    here = Path(__file__).resolve().parent
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "out"
    problems: list = []
    docs = []
    if target.is_dir():
        items = json.loads((here / "batch.json").read_text())["items"]
        for iid in items:
            f = target / f"{iid}.json"
            if not f.exists():
                problems.append(f"{iid}: out file missing")
                continue
            try:
                docs.append(json.loads(f.read_text()))
            except json.JSONDecodeError as e:
                problems.append(f"{iid}: invalid JSON ({e})")
    else:
        raw = target.read_text().strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw)
        docs = json.loads(raw)["documents"]
    for d in docs:
        check_doc(here, d, problems)
    for p in problems:
        print(p)
    print("FAIL" if problems else f"OK ({len(docs)} documents)")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
