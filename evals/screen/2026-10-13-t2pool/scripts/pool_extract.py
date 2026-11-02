"""Extract P5b's tier-2 escalations from store-p5-dev (read-only): tier-1 and tier-2 rows, whole texts."""
import duckdb, glob, os, json, gzip, hashlib, sys
from pathlib import Path
OUT = Path(sys.argv[1])
st = Path("v4/store-p5-dev")
ev = sorted(glob.glob(str(st / "data/*screen_evidence*")), key=os.path.getmtime)[-1]
corp = sorted(glob.glob(str(st / "data/*fold.corpus*")), key=os.path.getmtime)[-1]
c = duckdb.connect()
t2 = c.sql(f"select * exclude (decided_at, first_attempt_at, last_attempt_at, retry_after) from '{ev}' where prompt_name='screen-v11' and adjudication_tier=2 and status='ok'").fetchall()
cols = [d[0] for d in c.sql(f"select * exclude (decided_at, first_attempt_at, last_attempt_at, retry_after) from '{ev}' limit 0").description]
t2 = [dict(zip(cols, r)) for r in t2]
docs = sorted({r["doc_id"] for r in t2})
print(len(t2), len(docs))
rows = []
for d in docs:
    r2 = [r for r in t2 if r["doc_id"] == d][-1]
    r1 = c.sql(f"select * exclude (decided_at, first_attempt_at, last_attempt_at, retry_after) from '{ev}' where evidence_id = ?", params=[r2["supersedes"]]).fetchall()
    r1 = dict(zip(cols, r1[0]))
    cr = c.sql(f"select metadata.title, metadata.doc_type, identity.members[1].channel, content.current_text_digest, content.current_role, identity.members[1].license_class from '{corp}' where doc_id=?", params=[d]).fetchall()[0]
    title, dt, ch, dig, role, lic = cr
    assert dig == r1["basis_digest"], (d, dig, r1["basis_digest"])
    text = (st / "blobs/text" / dig).read_bytes().decode("utf-8")
    (OUT / "texts").mkdir(parents=True, exist_ok=True)
    (OUT / "texts" / f"{d}.txt").write_text(text, "utf-8")
    def q(r):
        return {"verdict": r["verdict"], "rationale": r["reasoning"], "stack": r["stack_relevance"] and {k: r["stack_relevance"][k] for k in ("verdict", "confidence")},
                "sr": r["sr_relevance"] and {k: r["sr_relevance"][k] for k in ("verdict", "confidence", "category")},
                "model": r["model"], "cost_usd": r["cost_usd"], "tokens_in": r["tokens_in"], "tokens_out": r["tokens_out"]}
    rows.append({"doc_id": d, "title": title, "doc_type": dt, "channel": ch, "basis_role": role, "license_class": lic,
                 "text_digest": dig, "chars": len(text), "tier1": q(r1), "tier2": q(r2)})
(OUT / "pool.json").write_text(json.dumps(rows, indent=1, default=str), "utf-8")
for r in rows:
    print(r["doc_id"], r["channel"], r["chars"], r["tier1"]["stack"]["verdict"], r["tier1"]["sr"]["verdict"], "->", r["tier2"]["verdict"], r["tier2"]["stack"]["verdict"], r["tier2"]["sr"]["verdict"], round(r["tier2"]["cost_usd"], 4), r["title"][:60])
