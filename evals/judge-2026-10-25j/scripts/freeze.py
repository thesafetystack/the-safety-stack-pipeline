"""Freeze the judge eval sets 2026-10-25j: per item a private judge task dir, and the releasable item records.

    freeze.py [--sets triage,screen,...]

Reads ``<set>/sample.jsonl`` (sample.py), the build's heads (EVAL104_DATA) and text blobs (EVAL104_BLOBS_GLOB).
Writes, read only towards the store:

* ``EVAL104_PRIVATE/tasks/<set>/<item_id>[-<call>]/``: exactly the files of ``common.TASK_FILES[call]`` (the guide,
  the schema, the document: ``text.txt`` = the current text blob byte for byte, or for triage ``record.txt`` = the
  rendered record luna was sent; ``meta.md``; set-specific shared files). Nothing about production output. An
  existing task dir is replaced.
* ``<set>/items.jsonl`` (here, releasable): ids, stratum and weight, metadata, digests of every task file, luna's
  input window (what production read of the text, for a window-vs-judgement split), the verification of the
  frozen input against production's evidence row.
* ``<set>/production.jsonl`` (here, releasable): the production labels (quotes capped at 300 characters).
* ``<set>/manifest.json``; ``guides/`` (every guide variant, the shared files, schemas, the wrapper prompts).

**Input verification.** Triage: the record is rebuilt with production's own functions
(``workflows/triage/ask.py`` ``record_text`` / ``title_record_text`` on the gate-view row, excerpt or OpenAlex
subjects and query labels) and its ``rendered_digest`` (``stack.llm.rendered_digest`` over the template, schema and
the classify_1k params, and the record's input digest) is compared with the evidence row's. Corpus sets: the
annotation's ``basis_digest`` must be the current text sha256 (the text the judge reads is the text production read)
and, where production's first call can be rebuilt (screen tier 1, affiliation, model_roles, technique_staged's
first call), its ``rendered_digest`` is compared too. Mismatches are recorded per item and summarised, not hidden.
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C  # noqa: E402
import guides as G  # noqa: E402

QUOTE_CAP = 300
CHECK_ONLY = False
SCREEN_PIN = {"name": "screen-v12", "model": "openai/gpt-6-luna", "adjudicate_model": "openai/gpt-6-luna"}
MR_CAP = 900_000  # plan pin doc_cap of annotate.model_roles and annotate.technique_staged (batch-2026-10a rev. 7)
TS_CAP = 900_000


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


class BlobAdapter:
    """``stack.store.blobs``-like ``get`` over :class:`common.Blobs` (raises ``BlobNotFound``)."""

    def __init__(self, blobs: C.Blobs) -> None:
        self.b = blobs

    def get(self, digest: str) -> bytes:
        from stack.store.blobs import BlobNotFound

        try:
            return self.b.get(digest)
        except KeyError as exc:
            raise BlobNotFound(f"blob {digest} not in <mirror>") from exc


def rows_by_id(con: Any, path: Path, ids: list[str]) -> tuple[dict[str, dict[str, Any]], Any]:
    con.execute("create or replace temp table want(doc_id varchar)")
    con.executemany("insert into want values (?)", [(d,) for d in ids])
    t = con.execute(f"select p.* from read_parquet('{path}') p join want w using (doc_id)").to_arrow_table()
    return {r["doc_id"]: r for r in t.to_pylist()}, t


def evidence_by_id(con: Any, path: Path, ids: list[str]) -> dict[str, dict[str, Any]]:
    con.execute("create or replace temp table want_ev(evidence_id varchar)")
    con.executemany("insert into want_ev values (?)", [(d,) for d in ids if d])
    t = con.execute(f"select p.* from read_parquet('{path}') p join want_ev w using (evidence_id)").to_arrow_table()
    return {r["evidence_id"]: r for r in t.to_pylist()}


def cap_quotes(obj: Any) -> Any:
    """Every verbatim string (:data:`common.VERBATIM_KEYS`, lists under them included) cut to :data:`QUOTE_CAP`
    characters at a word boundary with "…" (the dataset release's rule, ``stack.fold.dataset.cut_string``)."""
    return C.cap_verbatim(_plain(obj), QUOTE_CAP)


def _plain(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_plain(x) for x in obj]
    if isinstance(obj, (dt.datetime, dt.date)):
        return obj.isoformat()
    return obj


def rendered(template: str, schema: dict[str, Any], role: str, model: str, max_tokens: int | None, user: str,
             prior: list[dict[str, str]] | None = None) -> str:
    """Production's ``rendered_digest`` of one call (``stack.llm``): template, schema, the role's params at
    ``max_tokens`` (None: the role's), the user message."""
    from stack.annotate.stage import stack_config
    from stack.core import hashes
    from stack.llm import conversation_input, render_call, rendered_digest

    rc = stack_config().llm.roles[role]
    r = render_call(template, {}, model=model, max_tokens=max_tokens or rc.max_tokens, schema=schema,
                    temperature=rc.temperature, reasoning_effort=rc.reasoning_effort)
    return rendered_digest(r.rendered_hash, hashes.input_digest(conversation_input(prior or (), user)))


def meta_md(row: dict[str, Any], text: str) -> str:
    md = row["metadata"]
    names = [a["name"] for a in md.get("authors") or [] if a.get("name")]
    who = ", ".join(names[:40]) + (f" (+{len(names) - 40} more)" if len(names) > 40 else "")
    pub = md.get("published_at")
    chans = ", ".join(row["identity"].get("channels") or []) or "unknown"
    return (f"# Document metadata\n\n- title: {md.get('title') or 'unknown'}\n- authors: {who or 'unknown'}\n"
            f"- published: {pub.year if pub else 'unknown'}\n- source channels: {chans}\n"
            f"- document length: {len(text):,} characters, {text.count(chr(10)) + 1:,} lines (text.txt)\n")


def write_task(set_name: str, item_id: str, call: str, files: dict[str, bytes]) -> dict[str, str]:
    want = set(C.TASK_FILES[call])
    if set(files) != want:
        raise SystemExit(f"{set_name}/{item_id}/{call}: task files {sorted(files)} != {sorted(want)}")
    d = C.task_dir(set_name, item_id, call)
    if CHECK_ONLY:  # --check-tasks: the dir must already hold exactly these bytes; nothing is written
        have = {n: C.sha256_bytes((d / n).read_bytes()) for n in sorted(files)} if d.is_dir() else {}
        want_d = {n: C.sha256_bytes(b) for n, b in sorted(files.items())}
        if have != want_d or sorted(p.name for p in d.iterdir()) != sorted(files):
            raise SystemExit(f"--check-tasks: {d.name} differs from what the freeze would write")
        return want_d
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    for n, b in files.items():
        (d / n).write_bytes(b)
    return {n: C.sha256_bytes(b) for n, b in sorted(files.items())}


def jbytes(obj: Any) -> bytes:
    return (json.dumps(obj, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


def base_item(s: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    md = row["metadata"]
    pub = md.get("published_at")
    return {"item_id": s["item_id"], "doc_id": s["doc_id"], "set_id": C.SET_ID, "stratum": s["stratum"],
            "weight": s["weight"], "inclusion_prob": s["inclusion_prob"],
            "metadata": {"title": md.get("title"), "authors_n": len(md.get("authors") or []),
                         "year": pub.year if pub else None, "channels": row["identity"].get("channels") or [],
                         "anchor_channel": s.get("channel"), "collector_doc_type": md.get("doc_type")},
            "license_class": s.get("license_class")}


# ── triage ────────────────────────────────────────────────────────────────────────────────────


def source_labels(con: Any, source_ids: set[str]) -> dict[str, list[str]]:
    """``source_id`` -> its ``metadata.matched_by`` labels (else query ids), over every ``sources.*`` head file, as
    ``workflows/triage/ask.py`` ``match_labels`` reads them."""
    out: dict[str, list[str]] = {}
    if not source_ids:
        return out
    con.execute("create or replace temp table want_src(source_id varchar)")
    con.executemany("insert into want_src values (?)", [(x,) for x in sorted(source_ids)])
    for f in sorted(C.data_dir().glob(C.SOURCES_GLOB)):
        try:
            rows = con.execute(f"select p.source_id, p.metadata.matched_by from read_parquet('{f}') p "
                               f"join want_src w using (source_id)").fetchall()
        except Exception as exc:  # a sources table without matched_by: no labels from it
            if "matched_by" in str(exc):
                continue
            raise
        for sid, ms in rows:
            labels = [str(m.get("label") or m.get("query_id")) for m in ms or [] if m.get("label") or m.get("query_id")]
            if labels:
                out[sid] = list(dict.fromkeys(out.get(sid, []) + labels))
    return out


def freeze_triage(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[list, list, dict]:
    from stack import gate
    from stack.core.hashes import input_digest
    from workflows.triage import ask as ta

    rows, table = rows_by_id(con, C.data_file("documents"), [s["doc_id"] for s in sample])
    view = {r["doc_id"]: r for r in gate.gate_view(table, gate.channel_groups()).to_pylist()}
    evs = evidence_by_id(con, C.data_file("triage_evidence"), [s["evidence_id"] for s in sample])
    ctx = SimpleNamespace(blobs=BlobAdapter(blobs))
    title_srcs = {m["source_id"] for s in sample if s["basis"] == "title"
                  for m in view[s["doc_id"]]["identity"]["members"] or [] if m["is_live"]}
    labels = source_labels(con, title_srcs)
    schema = G.triage_schema()
    guide = {b: G.all_guides()[f"triage-{b}.md"].encode() for b in ("fields", "title")}
    items, prod, stats = [], [], collections.Counter()
    for s in sample:
        r, ev = view[s["doc_id"]], evs[s["evidence_id"]]
        name = ev["prompt_name"]
        if ev["basis"] == "title":
            subj = ta._subjects(ctx, ta.topic_record_sha256(r))
            live = [m["source_id"] for m in r["identity"]["members"] or [] if m["is_live"]]
            found = list(dict.fromkeys(x for sid in live for x in labels.get(sid, [])))
            record = ta.title_record_text(name, r, subj, found)
        else:
            record = ta.record_text(name, r, ta._excerpt(ctx, r.get("inband_text_sha256")))
        template = (G.W / "triage" / f"{name}.md").read_text("utf-8")
        ok = rendered(template, ta.SCHEMA, "classify_1k", ev["model"], None, record) == ev["rendered_digest"]
        stats[f"rendered_digest_{'match' if ok else 'MISMATCH'}"] += 1
        shown, breaks = C.wrap_long_lines(record)  # Read truncates lines > 2,000 chars; spaces -> newlines only
        stats["record_wrapped"] += bool(breaks)
        files = {"guide.md": guide[ev["basis"]], "schema.json": jbytes(schema), "record.txt": shown.encode("utf-8")}
        digests = write_task("triage", s["item_id"], "triage", files)
        it = base_item(s, r)
        it.update(calls=["triage"], basis=ev["basis"], prompt_name=name,
                  input={"record_sha256": C.sha256_text(record), "record_chars": len(record), "record_wrap_breaks": breaks,
                         "input_digest": input_digest(record)},
                  luna_window={"kind": "record", "note": "the judge sees the same record (the whole input)"},
                  verification={"rendered_digest_match": ok, "evidence_id": ev["evidence_id"]},
                  task_files={"triage": digests})
        items.append(it)
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "triage", "model": ev["model"],
                     "prompt_name": name, "basis": ev["basis"], "evidence_id": ev["evidence_id"],
                     "label": {"verdict": ev["verdict"], "confidence": ev["confidence"], "reasoning": ev["reasoning"]}})
    return items, prod, dict(stats)


# ── corpus sets ───────────────────────────────────────────────────────────────────────────────


def corpus_rows(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[dict, dict[str, bytes]]:
    rows, _ = rows_by_id(con, C.data_file("corpus"), [s["doc_id"] for s in sample])
    texts = {}
    for s in sample:
        dig = rows[s["doc_id"]]["content"]["current_text_sha256"]
        raw = blobs.get(dig)
        if C.sha256_bytes(raw) != dig:
            raise SystemExit(f"{s['item_id']}: blob content does not match its name")
        raw.decode("utf-8")  # production's strict decode (stack.annotate.stage._text): fail loudly here
        texts[s["doc_id"]] = raw
    return rows, texts


def text_file(raw: bytes, stats: collections.Counter) -> bytes:
    """``text.txt``: the blob with lines over ``common.WRAP_AT`` characters broken at spaces (offsets unchanged)."""
    w, breaks = C.wrap_long_lines(raw.decode("utf-8"))
    stats["text_wrapped_docs"] += bool(breaks)
    stats["text_wrap_breaks"] += breaks
    return w.encode("utf-8")


def annotation(row: dict[str, Any], name: str) -> dict[str, Any]:
    return row["annotations"][name] or {}


def basis_check(row: dict[str, Any], name: str) -> bool:
    return annotation(row, name).get("basis_digest") == row["content"]["current_text_sha256"]


def freeze_screen(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[list, list, dict]:
    import workflows.annotate.screen.main as sm

    rows, texts = corpus_rows(con, sample, blobs)
    guides = G.all_guides()
    gfile = {"relevance": guides["screen-relevance.md"].encode(), "quality": guides["screen-quality.md"].encode()}
    schemas = {"relevance": jbytes(G.screen_relevance_schema()), "quality": jbytes(G.screen_quality_schema())}
    limits = sm.input_limits(SCREEN_PIN)
    # tier-1 evidence rows: the doc's ok rows of adjudication tier 1 on its current basis
    con.execute("create or replace temp table want(doc_id varchar)")
    con.executemany("insert into want values (?)", [(s["doc_id"],) for s in sample])
    t1 = {}
    for r in con.execute(f"select p.* from read_parquet('{C.data_file('screen_evidence')}') p join want using (doc_id) "
                         f"where p.status = 'ok' and p.adjudication_tier = 1 order by p.decided_at").to_arrow_table().to_pylist():
        t1[(r["doc_id"], r["basis_digest"])] = r  # newest last
    tmpl = sm.template("screen-v12")
    spec = sm.spec("screen-v12")
    items, prod, stats = [], [], collections.Counter()
    for s in sample:
        row, raw = rows[s["doc_id"]], texts[s["doc_id"]]
        text = raw.decode("utf-8")
        scr = annotation(row, "screen")
        p = scr.get("payload") or {}
        tier = p.get("adjudication_tier")
        w1 = sm.screening_text(text, limits.tier1)
        win = {"tier1": {"limit": list(limits.tier1), "shown_chars": w1[1], "whole": w1[1] >= len(text)},
               "adjudication_tier": tier}
        if tier == 2:
            w2 = sm.screening_text(text, limits.tier2)
            win["tier2"] = {"limit": list(limits.tier2), "shown_chars": w2[1], "whole": w2[1] >= len(text)}
        final = win.get("tier2", win["tier1"])
        win["final_whole"] = final["whole"]
        ok_basis = all(basis_check(row, a) for a in ("screen", "stack_relevance", "sr_relevance", "text_usability",
                                                      "doc_class"))
        ev1 = t1.get((s["doc_id"], row["content"]["current_text_sha256"]))
        ok_rd, ask_title = None, None
        if ev1 is not None:
            def t1_ok(title: str | None) -> bool:
                user = (f"Title: {title or 'Unknown'}\n\n" + "Screening text (abstract or document excerpt):\n" + w1[0])
                return rendered(tmpl, spec.schema, sm.ROLE, ev1["model"], spec.tier1_max_tokens, user) == ev1["rendered_digest"]
            ok_rd = t1_ok(row["metadata"]["title"])
            if not ok_rd:  # the title metadata changed after the ask (a text-basis carry does not re-ask): try the
                # members' and revisions' titles; record the one production sent when it verifies
                cands = [m.get("title") for m in row["identity"]["members"] or []]
                cands += [r.get("title") for r in (row["content"].get("revisions") or [])]
                for t in dict.fromkeys(c for c in cands if c and c != row["metadata"]["title"]):
                    if t1_ok(t):
                        ok_rd, ask_title = True, t
                        stats["tier1_title_recovered"] += 1
                        break
        stats[f"basis_{'ok' if ok_basis else 'MISMATCH'}"] += 1
        stats[f"tier1_rendered_digest_{ {True: 'match', False: 'MISMATCH', None: 'no_tier1_row'}[ok_rd] }"] += 1
        meta = meta_md(row, text).encode()
        tf = text_file(raw, stats)
        digests = {c: write_task("screen", s["item_id"], c, {"guide.md": gfile[c], "schema.json": schemas[c],
                                                             "text.txt": tf, "meta.md": meta})
                   for c in ("relevance", "quality")}
        channel_type = sm.channel_doc_type(row)
        paper_type = sm.metadata_paper_type(row)
        it = base_item(s, row)
        it.update(calls=["relevance", "quality"],
                  input={"text_sha256": row["content"]["current_text_sha256"], "chars": len(text),
                         "basis_role": row["content"]["current_role"], "usability_production": s.get("usability")},
                  luna_window=win,
                  doc_class_rule={"channel_doc_type": channel_type, "metadata_paper_type": paper_type,
                                  "model_judgement": channel_type is None,
                                  "note": "doc_type is fixed by the channel when channel_doc_type is set (not scored); "
                                          "a paper/preprint answer takes metadata_paper_type when that is set"},
                  verification={"basis_digest_current": ok_basis, "tier1_rendered_digest_match": ok_rd,
                                "tier1_ask_time_title": ask_title,
                                "evidence_id": scr.get("evidence_id")},
                  task_files=digests)
        items.append(it)
        ann = {a: cap_quotes((annotation(row, a).get("payload") or {})) for a in
               ("screen", "stack_relevance", "sr_relevance", "text_usability", "doc_class")}
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "relevance", "model": p.get("tier1_model"),
                     "prompt_name": "screen-v12",
                     "label": {"verdict": p.get("verdict"), "buckets": p.get("buckets"),
                               "off_topic_reason": p.get("off_topic_reason"), "confidence": p.get("confidence"),
                               "reasoning": p.get("reasoning"), "adjudication_tier": tier,
                               "tier1_verdict": p.get("tier1_verdict"), "quotes": ann["screen"].get("quotes"),
                               "stack": ann["stack_relevance"], "sr": ann["sr_relevance"]}})
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "quality", "model": p.get("tier1_model"),
                     "prompt_name": "screen-v12",
                     "label": {"usability": ann["text_usability"], "doc_class": ann["doc_class"]}})
    return items, prod, dict(stats)


def freeze_affiliation(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[list, list, dict]:
    from stack.stage_runner import load_path_module

    main = load_path_module("_j104_aff_main", G.W / "annotate" / "affiliation" / "main.py")
    ex = main.ex
    rows, texts = corpus_rows(con, sample, blobs)
    evs = evidence_by_id(con, C.data_file("affiliation_evidence"),
                         [annotation(rows[s["doc_id"]], "affiliation").get("evidence_id") for s in sample])
    guide = G.all_guides()["affiliation.md"].encode()
    schema = jbytes(G.affiliation_schema())
    items, prod, stats = [], [], collections.Counter()
    for s in sample:
        row, raw = rows[s["doc_id"]], texts[s["doc_id"]]
        text = raw.decode("utf-8")
        roster = main.roster_of(row)
        block = ex.build_authors_block(roster)
        window = ex.header_window(text)
        a = annotation(row, "affiliation")
        ev = evs.get(a.get("evidence_id"))
        ok_rd, roster_source = None, "current_metadata"
        if ev is not None and ev.get("rendered_digest"):
            ok_rd = rendered(main.TEMPLATE, main.SCHEMA, "extract_main", ev["model"], None,
                             main._user_message(block, window)) == ev["rendered_digest"]
            if not ok_rd and roster:
                # the authors metadata changed after the ask (a text-basis carry does not re-ask on metadata): the
                # roster production sent is its items' author_surface in byline order; use it when it verifies
                alt = [x["author_surface"] for x in sorted(ev["items"] or [], key=lambda x: x["byline_index"])]
                alt_block = ex.build_authors_block(alt)
                if rendered(main.TEMPLATE, main.SCHEMA, "extract_main", ev["model"], None,
                            main._user_message(alt_block, window)) == ev["rendered_digest"]:
                    roster, block, ok_rd, roster_source = alt, alt_block, True, "evidence_items"
        stats[f"roster_{roster_source}"] += 1
        ok_basis = basis_check(row, "affiliation")
        stats[f"basis_{'ok' if ok_basis else 'MISMATCH'}"] += 1
        stats[f"rendered_digest_{ {True: 'match', False: 'MISMATCH', None: 'none'}[ok_rd] }"] += 1
        body = ex._SYNTHETIC_PREFIX_RE.sub("", text, count=1)
        whole = len(body) <= ex.HEAD_CHARS + ex.TAIL_CHARS
        files = {"guide.md": guide, "schema.json": schema, "text.txt": text_file(raw, stats), "meta.md": meta_md(row, text).encode(),
                 "authors.txt": (block + ("\n" if block else "")).encode("utf-8")}
        digests = write_task("affiliation", s["item_id"], "affiliation", files)
        it = base_item(s, row)
        it.update(calls=["affiliation"],
                  input={"text_sha256": row["content"]["current_text_sha256"], "chars": len(text),
                         "basis_role": row["content"]["current_role"], "roster_n": len(roster), "roster_source": roster_source,
                         "authors_block_sha256": C.sha256_text(block)},
                  luna_window={"kind": "header_window", "head": ex.HEAD_CHARS, "tail": ex.TAIL_CHARS,
                               "window_chars": len(window), "whole": whole},
                  verification={"basis_digest_current": ok_basis, "rendered_digest_match": ok_rd,
                                "evidence_id": a.get("evidence_id")},
                  task_files={"affiliation": digests})
        items.append(it)
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "affiliation",
                     "model": ev.get("model") if ev else None, "prompt_name": "affiliation-v1",
                     "label": cap_quotes(a.get("payload") or {})})
    return items, prod, dict(stats)


def freeze_model_roles(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[list, list, dict]:
    main = G.mr_main()
    c = main.match.scanner_for(C.REPO / "configs")
    pf = main.prompt_file(G.MR_PROMPT)
    rows, texts = corpus_rows(con, sample, blobs)
    evs = evidence_by_id(con, C.data_file("model_roles_evidence"),
                         [annotation(rows[s["doc_id"]], "model_roles").get("evidence_id") for s in sample])
    guides = G.all_guides()
    shared = {"guide.md": guides["model_roles.md"].encode(), "schema.json": jbytes(G.model_roles_schema()),
              "rubric.md": guides["model_roles-rubric.md"].encode(),
              "catalogue.tsv": guides["model_roles-catalogue.tsv"].encode()}
    items, prod, stats = [], [], collections.Counter()
    for s in sample:
        row, raw = rows[s["doc_id"]], texts[s["doc_id"]]
        text = raw.decode("utf-8")
        scan = c.scanner.scan(text)
        menu, beyond, fills = main.fills(text, scan, c.vocab, max(len(text), 1_000))  # judges: the whole text
        lmenu, lbeyond, user = main.user_message(pf, text, scan, c.vocab, MR_CAP)  # luna's
        a = annotation(row, "model_roles")
        ev = evs.get(a.get("evidence_id"))
        ok_rd = None
        if ev is not None:
            ok_rd = rendered(pf.system(), main.SCHEMA, "extract_main_openai", ev["model"], main.MAX_TOKENS,
                             user) == ev["rendered_digest"]
        ok_basis = basis_check(row, "model_roles")
        stats[f"basis_{'ok' if ok_basis else 'MISMATCH'}"] += 1
        stats[f"rendered_digest_{ {True: 'match', False: 'MISMATCH', None: 'none'}[ok_rd] }"] += 1
        head = (f"# Model menu\n\nCandidate catalogue models the production matcher found in this document "
                f"({len(menu)} entries" + (", none: the menu is empty" if not menu else "") + ").\n")
        files = dict(shared, **{"text.txt": text_file(raw, stats), "meta.md": meta_md(row, text).encode(),
                                "menu.md": (head + fills["menu"]).encode("utf-8")})
        digests = write_task("model_roles", s["item_id"], "model_roles", files)
        it = base_item(s, row)
        it.update(calls=["model_roles"],
                  input={"text_sha256": row["content"]["current_text_sha256"], "chars": len(text),
                         "basis_role": row["content"]["current_role"],
                         "menu": [{"node_id": e.node_id, "weak": e.weak} for e in menu]},
                  luna_window={"kind": "doc_cap", "cap": MR_CAP, "whole": len(text) <= MR_CAP,
                               "menu_n": len(lmenu), "beyond_window": lbeyond},
                  verification={"basis_digest_current": ok_basis, "rendered_digest_match": ok_rd,
                                "evidence_id": a.get("evidence_id")},
                  task_files={"model_roles": digests})
        items.append(it)
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "model_roles",
                     "model": ev.get("model") if ev else None, "prompt_name": G.MR_PROMPT, "status": a.get("status"),
                     "label": cap_quotes(a.get("payload") or {})})
    return items, prod, dict(stats)


def freeze_technique(con: Any, sample: list[dict[str, Any]], blobs: C.Blobs) -> tuple[list, list, dict]:
    main = G.ts_main()
    cat = main.catalogue(C.REPO / G.TS_TAXONOMY)
    rows, texts = corpus_rows(con, sample, blobs)
    evs = evidence_by_id(con, C.data_file("technique_staged_evidence"),
                         [annotation(rows[s["doc_id"]], "technique_staged").get("evidence_id") for s in sample])
    guides = G.all_guides()
    shared = {"guide.md": guides["technique_staged.md"].encode(), "schema.json": jbytes(G.technique_schema()),
              "rubric.md": guides["technique_staged-rubric.md"].encode(),
              "catalogue.md": guides["technique_staged-catalogue.md"].encode()}
    items, prod, stats = [], [], collections.Counter()
    for s in sample:
        row, raw = rows[s["doc_id"]], texts[s["doc_id"]]
        text = raw.decode("utf-8")
        a = annotation(row, "technique_staged")
        ev = evs.get(a.get("evidence_id"))
        # not checkable: the evidence row's rendered_digest is sha256 over [first call, gleaning call]
        # (main.merged_llm) and the gleaning call's prior turn embeds luna's own answer, which is not stored
        ok_rd = None
        ok_basis = basis_check(row, "technique_staged")
        stats[f"basis_{'ok' if ok_basis else 'MISMATCH'}"] += 1
        stats["rendered_digest_not_checkable_glean"] += 1
        files = dict(shared, **{"text.txt": text_file(raw, stats), "meta.md": meta_md(row, text).encode()})
        digests = write_task("technique_staged", s["item_id"], "technique_staged", files)
        it = base_item(s, row)
        it.update(calls=["technique_staged"],
                  input={"text_sha256": row["content"]["current_text_sha256"], "chars": len(text),
                         "basis_role": row["content"]["current_role"]},
                  luna_window={"kind": "doc_cap", "cap": TS_CAP, "whole": len(text) <= TS_CAP, "glean": True},
                  verification={"basis_digest_current": ok_basis, "rendered_digest_match": ok_rd, "rendered_digest_note": "not checkable: digest over the gleaning call",
                                "evidence_id": a.get("evidence_id")},
                  task_files={"technique_staged": digests})
        items.append(it)
        pl = dict(a.get("payload") or {})
        pl.pop("prepass", None)  # pipeline internals (segmenter, calls), not labels
        prod.append({"item_id": s["item_id"], "doc_id": s["doc_id"], "call": "technique_staged",
                     "model": ev.get("model") if ev else None, "prompt_name": G.TS_SET, "status": a.get("status"),
                     "taxonomy_version": cat.version, "label": cap_quotes(pl)})
    return items, prod, dict(stats)


FREEZERS = {"triage": freeze_triage, "screen": freeze_screen, "affiliation": freeze_affiliation,
            "model_roles": freeze_model_roles, "technique_staged": freeze_technique}


def write_guides() -> dict[str, str]:
    out = C.SET_ROOT / "guides"
    out.mkdir(exist_ok=True)
    sums = {}
    for n, t in G.all_guides().items():
        (out / n).write_text(t, "utf-8")
        sums[n] = C.sha256_text(t)
    for n, s in G.schemas().items():
        b = jbytes(s)
        (out / f"schema-{n}.json").write_bytes(b)
        sums[f"schema-{n}.json"] = C.sha256_bytes(b)
    for call in C.TASK_FILES:
        t = C.wrapper_prompt(call) + "\n"
        (out / f"prompt-{call}.txt").write_text(t, "utf-8")
        sums[f"prompt-{call}.txt"] = C.sha256_text(t)
    return sums


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--sets", default=",".join(C.SETS))
    ap.add_argument("--check-tasks", action="store_true",
                    help="write no task dir: check every one holds exactly the bytes the freeze would write, and "
                         "rewrite only items / production / manifest (e.g. after a change to the release capping)")
    a = ap.parse_args()
    global CHECK_ONLY
    CHECK_ONLY = a.check_tasks
    import duckdb

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'; SET threads=2")
    blobs = C.Blobs()
    gsums = write_guides()
    for set_name in a.sets.split(","):
        sample = C.read_jsonl(C.SET_ROOT / set_name / "sample.jsonl")
        items, prod, stats = FREEZERS[set_name](con, sample, blobs)
        isha = C.write_jsonl(C.SET_ROOT / set_name / "items.jsonl", items)
        psha = C.write_jsonl(C.SET_ROOT / set_name / "production.jsonl", prod)
        manifest = {"set_id": C.SET_ID, "set": set_name, "calls": list(C.CALLS[set_name]), "n_items": len(items),
                    "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
                    "build": "ws1qpn8myzxah", "data_files": {k: C.DATA_FILES[k] for k in C.DATA_FILES},
                    "items_sha256": isha, "production_sha256": psha,
                    "sample_sha256": C.sha256_file(C.SET_ROOT / set_name / "sample.jsonl"),
                    "verification": stats, "guides_sha256": gsums,
                    "prompts": {c: C.wrapper_prompt(c) for c in C.CALLS[set_name]},
                    "tasks": "EVAL104_PRIVATE/tasks/<set>/<item_id>[-<call>]/ (private: third-party texts)"}
        (C.SET_ROOT / set_name / "manifest.json").write_text(C.dumps(manifest), "utf-8")
        print(set_name, len(items), stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
