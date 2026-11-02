"""The eval-set freezer (track E, E3; ``stack/evals/freeze.py``) on the synthetic world
(``tests/synth/world.py``) with text blobs written for its digests and small quotas: a triage freeze
twice gives identical ``items.jsonl`` bytes; every record is ``workflows.triage.ask.document_text`` of
the item's gate-view row and excerpt (rebuilt here from the store) with a matching digest; masked,
merged and basis-less paper rows stay out; the split is balanced per stratum; packets carry only
``item_id`` + ``record``. The screen freeze captures the screen's own user message (truncated text) and
freezes each whole text as ``texts/<item_id>.txt.gz`` (same bytes on a re-freeze). The affiliation
freeze (hand-built rows: one with a JATS ``xml`` blob, one with a clear byline, one without) orders
XML first, freezes the v3 runner's user message, the XML byline and the roster orgs mentioned."""

from __future__ import annotations

import collections
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from stack import gate
from stack.evals import freeze as F
from stack.store.blobs import open_blobs
from stack.store.chain import read_view, resolve
from tests.synth.records import sdir
from tests.synth.world import build_world
from workflows.triage.ask import document_text

pytestmark = pytest.mark.evalsets  # area marker(s), not a tier (pyproject markers; docs/SETUP.md §7)

QUOTAS = {"paper": {"arxiv": 3, "openalex": 3}, "forum": {"*": 1}, "url": {"manual": 2}}
HARD = {"paper": 1, "url": 1}
LONG = 9_000  # one text over the screen's 8000-char cap


def _store(tmp: Path) -> tuple[Path, dict[str, str], dict[str, str], dict[str, str]]:
    """The world, plus a ``text`` blob for every in-band excerpt and current text digest (the world
    names digests without storing bodies). Returns the root, excerpt text by doc_id, text by doc_id
    and the channel groups."""
    root = tmp / "store"
    w = build_world(root)
    blobs = open_blobs(root)
    groups = gate.channel_groups()
    view = gate.gate_view(read_view(resolve(root, sdir("documents"))), groups).to_pylist()
    excerpts, texts, bodies = {}, {}, {}
    for i, r in enumerate(view):
        if r["inband_text_sha256"]:
            excerpts[r["doc_id"]] = f"In-band excerpt of {w.label(r['doc_id'])}: we red-team a monitor."
            bodies[r["inband_text_sha256"]] = excerpts[r["doc_id"]]
    for i, r in enumerate(view):
        cur = (r["content"] or {}).get("current_text_sha256")
        if cur:  # an in-band text that is current (O5) keeps its body
            body = f"Text of {w.label(r['doc_id'])} about alignment. "
            texts[r["doc_id"]] = bodies.setdefault(cur, (body * (LONG // len(body) + 1)) if i % 3 == 0 else body)
    for digest, body in bodies.items():
        p = blobs.local(digest)  # synthetic digests: labels, not hashes, so written in place
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.encode())
    return root, excerpts, texts, groups


def _read(p: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in p.read_text("utf-8").splitlines()]


@pytest.mark.slow
def test_freeze_triage_and_screen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, excerpts, texts, groups = _store(tmp_path)
    monkeypatch.setattr(F, "TRIAGE_QUOTAS", QUOTAS)
    monkeypatch.setattr(F, "TRIAGE_HARD", HARD)
    monkeypatch.setattr(F, "TRIAGE_PACKET", 4)

    a, ma = F.freeze("triage", F.load_source(root, sdir("documents")), "t1", tmp_path / "a", tmp_path / "pa",
                     "seed-x")
    src = F.load_source(root, sdir("documents"))  # one config load for the remaining calls (the test stays fast)
    F.freeze("triage", src, "t1", tmp_path / "b", tmp_path / "pb", "seed-x")
    assert (tmp_path / "a" / "items.jsonl").read_bytes() == (tmp_path / "b" / "items.jsonl").read_bytes()
    with pytest.raises(FileExistsError):  # a frozen set is not replaced silently
        F.freeze("triage", src, "t1", tmp_path / "a", tmp_path / "pa", "seed-x")
    items = _read(tmp_path / "a" / "items.jsonl")
    assert items == a and [it["item_id"] for it in items] == [f"tri-{i:04d}" for i in range(1, len(items) + 1)]

    # population: live rows only; paper/forum rows without a basis skipped; url rows as title-only probes
    table = read_view(resolve(root, sdir("documents")))
    rows = {r["doc_id"]: r for r in gate.gate_view(table, groups).to_pylist()}
    chosen = {it["doc_id"] for it in items}
    dead = {d for d, r in rows.items() if r["identity"]["is_masked"] or r["identity"]["merged_into"] is not None}
    assert len(dead) == 2 and {r["doc_id"] for r in src.rows} == set(rows) - dead  # X masked, O4 merged
    for d, r in rows.items():
        if r["identity"]["is_masked"] or r["identity"]["merged_into"] is not None:
            assert d not in chosen
        if r["channel_group"] == "paper" and not r["metadata"]["abstract"] and not r["inband_text_sha256"]:
            assert d not in chosen
    assert collections.Counter(it["stratum"]["channel_group"] for it in items) == {"paper": 6, "forum": 1, "url": 2}
    assert ma["selection"]["short"] == {"paper/openalex": 1}  # 2 openalex with a basis; arxiv took the 3rd
    assert collections.Counter(it["stratum"]["channel"] for it in items)["arxiv"] == 4
    assert [it["hard"] for it in items if it["stratum"]["channel_group"] == "url"].count("policy") == 1

    # every record is the production builder's output on the row and its excerpt, digest matching
    for it in items:
        r = rows[it["doc_id"]]
        excerpt = excerpts.get(it["doc_id"])
        assert it["input"]["excerpt"] == excerpt
        assert it["input"]["record"] == document_text(r, excerpt)
        assert it["input_digest"] == hashlib.sha256(it["input"]["record"].encode()).hexdigest()
        assert it["production_asks"] == bool(r["metadata"]["abstract"] or excerpt)
        assert it["license_class"] is not None
    forum = next(it for it in items if it["stratum"]["channel_group"] == "forum")
    assert "Excerpt (start of the text):\n" + excerpts[forum["doc_id"]] in forum["input"]["record"]
    assert {it["production_asks"] for it in items if it["stratum"]["channel_group"] == "url"} == {False}

    # split: balanced within one item per (group, proxy, hard?) stratum and overall
    strata: dict[tuple[str, str, bool], collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for it in items:
        strata[(it["stratum"]["channel_group"], it["stratum"]["proxy"], it["hard"] is not None)][it["split"]] += 1
    assert all(abs(c["dev"] - c["test"]) <= 1 for c in strata.values())
    total = collections.Counter(it["split"] for it in items)
    assert abs(total["dev"] - total["test"]) <= 1

    # packets: only item_id + record, item_id order, batches of TRIAGE_PACKET
    lines = [x for p in sorted((tmp_path / "pa").glob("batch-*.jsonl")) for x in _read(p)]
    assert len(sorted((tmp_path / "pa").glob("batch-*.jsonl"))) == -(-len(items) // 4)
    assert lines == [{"item_id": it["item_id"], "record": it["input"]["record"]} for it in items]
    assert ma["basis_builder"]["ref"] == "workflows.triage.ask:document_text"
    assert ma["documents_head"] == resolve(root, sdir("documents")).head
    assert ma["documents_in"] == sdir("documents") and ma["source_store"] == str(root)

    # screen: the dry run writes nothing; the freeze captures the screen's own user message
    _, pool = F.freeze_screen(src, "s1", tmp_path / "s", tmp_path / "ps", "seed-x", n=4, dry_run=True)
    assert not (tmp_path / "s").exists()
    assert sum(v["eligible"] for v in pool["pool"].values()) == len(texts)
    sitems, _ = F.freeze_screen(src, "s1", tmp_path / "s", tmp_path / "ps", "seed-x", n=4)
    assert len(sitems) == 4
    for it in sitems:
        full = texts[it["doc_id"]]
        sent = full if len(full) <= 8_000 else full[:8_000] + "\n…[truncated]"
        user = f"Title: {it['input']['title']}\n\nScreening text (abstract or document excerpt):\n{sent}"
        assert it["input"]["text"] == sent and it["input"]["full_text_chars"] == len(full)
        assert it["input_digest"] == hashlib.sha256(user.encode()).hexdigest()
    assert any(it["input"]["full_text_chars"] > 8_000 for it in sitems)
    slines = [x for p in sorted((tmp_path / "ps").glob("batch-*.jsonl")) for x in _read(p)]
    assert all(set(x) == {"item_id", "title", "text"} for x in slines) and len(slines) == 4
    assert [x["text"] for x in slines] == [it["input"]["text"] for it in sitems]  # the tier-1 text

    # the whole text, frozen beside the set: what tier 2 gets and quotes anchor against
    for it in sitems:
        full = texts[it["doc_id"]]
        assert F.read_text_gz(F.text_path(tmp_path / "s", it["item_id"])) == full
        assert it["input"]["full_text_digest"] == hashlib.sha256(full.encode()).hexdigest()
    assert len(list((tmp_path / "s" / "texts").glob("*.txt.gz"))) == 4
    F.freeze_screen(src, "s1", tmp_path / "s2", tmp_path / "ps2", "seed-x", n=4)
    for it in sitems:  # gzip with mtime 0: a re-freeze writes the same bytes
        data = F.text_path(tmp_path / "s", it["item_id"]).read_bytes()
        assert data[4:8] == b"\0\0\0\0" and data == F.text_path(tmp_path / "s2", it["item_id"]).read_bytes()

    # a holdout: another set's documents excluded, its own prefix, every item in one split (T-SCREEN)
    taken = frozenset(it["doc_id"] for it in sitems[:2])
    hitems, hman = F.freeze_screen(src, "h1", tmp_path / "h", tmp_path / "ph", "seed-h", n=4,
                                   exclude=taken, prefix="hold", split_all="holdout")
    assert not taken & {it["doc_id"] for it in hitems} and len(hitems) == min(4, len(texts) - 2)
    assert {it["split"] for it in hitems} == {"holdout"} and all(it["item_id"].startswith("hold-") for it in hitems)
    assert hman["excluded_doc_ids"] == 2


JATS = b"""<?xml version="1.0"?>
<article><front><article-meta>
<contrib-group>
<contrib contrib-type="author"><name><surname>Lee</surname><given-names>Ann</given-names></name>
<xref ref-type="aff" rid="a1">1</xref></contrib>
<contrib contrib-type="author"><name><surname>Chen</surname><given-names>Bo</given-names></name>
<xref ref-type="aff" rid="a2">2</xref></contrib>
</contrib-group>
<aff id="a1"><label>1</label>Anthropic, San Francisco</aff>
<aff id="a2"><label>2</label>University of Toronto</aff>
</article-meta></front><body><p>Body.</p></body></article>"""


def _aff_row(doc: str, text_sha256: str, authors: list[str], *, xml: str | None = None,
             group: str = "paper", role: str = "full_text") -> dict[str, Any]:
    """``xml``: the blob sha256 of :data:`JATS`, the row's raw ``xml`` snapshot."""
    snaps = [] if xml is None else [{"kind": "raw", "content_role": "xml",
                                     "raw": {"sha256": xml, "bytes": len(JATS), "media_type": "application/xml"},
                                     "text": None, "captured_at": "2026-10-12 10:00:00"}]
    return {"doc_id": doc, "channel_group": group,
            "identity": {"anchor_source_id": f"s-{doc}", "members": [
                {"source_id": f"s-{doc}", "channel": "arxiv", "license_class": "open", "is_live": True}]},
            "metadata": {"title": f"Title {doc}", "authors": [{"name": n} for n in authors],
                         "fields_digest": "f" * 64},
            "content": {"current_text_sha256": text_sha256, "current_role": role,
                        "current_source_id": f"s-{doc}", "snapshots": snaps}}


def test_freeze_affiliation(tmp_path: Path) -> None:
    from stack.evals.gold import affiliation_runner

    main = affiliation_runner()
    blobs = open_blobs(tmp_path)
    body = "\n\nAbstract. We study things. " + "Lorem ipsum. " * 1200  # over head + tail: a cut window
    t_xml = blobs.put(("# Paper X\nAnn Lee1 Bo Chen2\n1 Anthropic 2 University of Toronto" + body).encode())
    t_by = blobs.put(("Cy Diaz, Dee Evans\nGoogle DeepMind\ncy@deepmind.com" + body).encode())
    t_no = blobs.put(("Some report without names" + body).encode())
    raw = blobs.put(JATS)
    rows = [_aff_row("d-xml", t_xml, ["Ann Lee", "Bo Chen"], xml=raw),
            _aff_row("d-by", t_by, ["Diaz, Cy", "Dee Evans"]),
            _aff_row("d-no", t_no, ["Eve Fox"]),
            _aff_row("d-abs", t_no, ["Gil Hu"], role="abstract"),       # not a full text: out
            _aff_row("d-url", t_no, ["Hal Ito"], group="url"),          # not a paper: out
            _aff_row("d-html", t_no, ["Ida Jo"], role="html")]          # arXiv HTML only (D485): in
    src = F.Source(tmp_path, "h1.documents", rows, blobs, {"rows": 6, "live": 6}, "data/documents/main/")

    _, pool = F.freeze_affiliation(src, "a1", tmp_path / "a", tmp_path / "pa", "seed", dry_run=True)
    assert not (tmp_path / "a").exists()
    assert {k: pool["pool"][k] for k in ("eligible", "xml", "byline", "no_byline", "xml_byline_parsed")} == {
        "eligible": 4, "xml": 1, "byline": 1, "no_byline": 2, "xml_byline_parsed": 1}
    assert pool["pool"]["paper_without_full_text"] == 1  # the abstract-only row

    items, m = F.freeze_affiliation(src, "a1", tmp_path / "a", tmp_path / "pa", "seed", n=2)
    assert {it["doc_id"] for it in items} == {"d-xml", "d-by"}  # xml first, then the clear byline
    by = {it["doc_id"]: it for it in items}
    x, b = by["d-xml"], by["d-by"]
    assert x["stratum"]["proxy"] == "xml" and b["stratum"]["proxy"] == "byline"
    text = blobs.get(t_by).decode()
    window = main.ex.header_window(text)
    assert window != text and b["input"]["window"] == window
    assert b["input"]["authors_meta"] == ["Diaz, Cy", "Dee Evans"]
    assert b["input"]["user"] == main._user_message("Diaz, Cy\nDee Evans", window)
    assert b["input_digest"] == hashlib.sha256(b["input"]["user"].encode()).hexdigest()
    assert b["input"]["xml_byline"] is None and b["production_uses_llm"] is True
    assert [e["id"] for e in b["input"]["roster_entries"]] == ["deepmind"]
    assert x["production_uses_llm"] is False and x["production_asks"] is False
    assert x["input"]["xml_byline"] == [
        {"author_surface": "Ann Lee", "email": None, "orcid": None,
         "affiliations": [{"surface": "Anthropic, San Francisco", "marker": "1"}]},
        {"author_surface": "Bo Chen", "email": None, "orcid": None,
         "affiliations": [{"surface": "University of Toronto", "marker": "2"}]}]
    assert "<contrib-group>" in x["input"]["xml_snippet"] and "<body>" not in x["input"]["xml_snippet"]
    assert '<aff id="a2">' in x["input"]["xml_snippet"]
    assert [e["id"] for e in x["input"]["roster_entries"]] == ["anthropic"]
    assert m["basis_builder"]["caps"]["HEAD_CHARS"] == main.ex.HEAD_CHARS
    lines = [y for p in sorted((tmp_path / "pa").glob("batch-*.jsonl")) for y in _read(p)]
    assert [set(y) for y in lines] == [{"item_id", "window", "authors_meta", "xml_byline", "roster_entries"}] * 2


def test_freeze_affiliation_doc_ids(tmp_path: Path) -> None:
    """``doc_ids`` takes exactly those pool documents (any proxy, ignoring ``n``), refuses ids outside
    the pool and repeats, and ``split`` puts every item on one side; the manifest records both."""
    blobs = open_blobs(tmp_path)
    body = "\n\nAbstract. We study things. " + "Lorem ipsum. " * 50
    t_by = blobs.put(("Cy Diaz, Dee Evans\nGoogle DeepMind" + body).encode())
    t_no = blobs.put(("Some report without names" + body).encode())
    rows = [_aff_row("d-by", t_by, ["Cy Diaz", "Dee Evans"]), _aff_row("d-no", t_no, ["Eve Fox"]),
            _aff_row("d-no2", t_no, ["Fay Gu"]), _aff_row("d-abs", t_no, ["Gil Hu"], role="abstract")]
    src = F.Source(tmp_path, "h1.documents", rows, blobs, {"rows": 4, "live": 4}, "data/documents/main/")

    for bad in (["d-no", "d-abs"], ["d-no", "d-missing"], ["d-no", "d-no"], []):
        with pytest.raises(ValueError):
            F.freeze_affiliation(src, "a1", tmp_path / "x", tmp_path / "px", "seed", doc_ids=bad)
    assert not (tmp_path / "x").exists()
    with pytest.raises(ValueError):
        F.freeze_affiliation(src, "a1", tmp_path / "x", tmp_path / "px", "seed", split="train")

    # n=1 would take only the clear byline; doc_ids takes the two no_byline rows and nothing else
    items, m = F.freeze_affiliation(src, "a1", tmp_path / "a", tmp_path / "pa", "seed", n=1,
                                    doc_ids=["d-no2", "d-no"], split="test")
    assert sorted(it["doc_id"] for it in items) == ["d-no", "d-no2"]
    assert {it["split"] for it in items} == {"test"}
    assert [it["item_id"] for it in items] == ["aff-0001", "aff-0002"]
    assert m["doc_ids"] == ["d-no2", "d-no"] and m["split_forced"] == "test" and "target_n" not in m
    assert m["counts"]["split"] == {"test": 2}
    assert _read(tmp_path / "a" / "items.jsonl") == items
    assert sorted(y["item_id"] for p in (tmp_path / "pa").glob("batch-*.jsonl") for y in _read(p)) == [
        "aff-0001", "aff-0002"]


@pytest.mark.parametrize("app, argv", [
    ("freeze", ["triage", "--set", "t1", "--out", "o", "--packets", "p"]),
    ("freeze", ["screen", "--set", "s1", "--out", "o", "--packets", "p", "--dry-run"]),
    ("freeze", ["affiliation", "--set", "a1", "--out", "o", "--packets", "p", "--dry-run"]),
    ("sr", ["recall", "--cache", "c"]),
    ("sr", ["candidates", "--cache", "c", "--queries", "q.json", "--out", "o.jsonl"]),
    ("sr", ["append", "--cache", "c", "--task", "triage", "--set", "t1", "--picks", "p.json", "--packets", "p"]),
])
def test_cli_requires_in(tmp_path: Path, app: str, argv: list[str]) -> None:
    """No implicit documents series (D382): every freezer and SR builder verb refuses to run without
    ``--in``, before reading the store."""
    from typer.testing import CliRunner

    from stack.evals import sr

    cli = {"freeze": F.app, "sr": sr.app}[app]
    res = CliRunner().invoke(cli, [argv[0], "--store", str(tmp_path), *argv[1:]])
    assert res.exit_code == 2, res.output
    assert "Missing option" in res.output and "--in" in res.output


def test_cli_in_names_the_series(tmp_path: Path) -> None:
    """``--in`` is what the freezer reads: the store's series directory works (a screen dry run), a
    series the store does not have fails loudly instead of falling back to another one."""
    from typer.testing import CliRunner

    root, _, texts, _ = _store(tmp_path)
    base = ["screen", "--store", str(root), "--set", "s1", "--out", str(tmp_path / "s"),
            "--packets", str(tmp_path / "ps"), "--dry-run"]
    ok = CliRunner().invoke(F.app, [*base, "--in", sdir("documents")])
    assert ok.exit_code == 0, ok.output
    assert f"documents_head: {resolve(root, sdir('documents')).head}" in ok.output
    assert f"pool total: {len(texts)}" in ok.output
    bad = CliRunner().invoke(F.app, [*base, "--in", "data/documents/weekly/"])
    assert bad.exit_code != 0 and not (tmp_path / "s").exists()
