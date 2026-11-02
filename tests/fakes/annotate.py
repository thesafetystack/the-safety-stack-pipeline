"""TEST-ONLY workflows on the annotate framework (``stack.annotate``), driven by the fake model
(``tests/fakes/llm.py``): a one-tier ``screen`` (document grain, ``SCREEN`` payload) and an
``affiliation`` (item grain, ``AFFILIATION`` payload). They are not T4.3b's screen or T4.4's
affiliation; they exist to drive the framework through the real stage runner. Their stage
modules live under ``tests/fakes/annotate_stages/`` (select with ``STACK_WORKFLOWS_ROOT``).

Both ask the ``classify`` role (``configs/llm.yaml``: ``openai/gpt-6-luna``, priced) with the document's
title and current text; the fake answers by a substring of that text.
"""

from __future__ import annotations

import datetime as _dt
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pyarrow as pa

from stack.annotate import Answer, AskContext, AskItem, PromptPin, Workflow
from stack.core.hashes import template_hash
from stack.plan import Plan, StageLine, load_plan
from stack.runs import RunRecord, file_digest, find_run_record
from stack.stage_runner import run_stage
from stack.store.blobs import open_blobs
from stack.store.chain import head_of
from stack.store.parquet import read_family, write_family
from tests.fakes.overrides import overrides_input
from tests.synth.records import SERIES, rewrite_record, sdir
from tests.synth.rows import document_row, rid, with_digests
from tests.synth.world import write_on_head

ROLE = "classify"
SCREEN_TEMPLATE = "Screen the document for AI-evaluation relevance.\n{{ref:menu}}\nJSON only."
SCREEN_REFS = {"menu": "verdict: relevant | not_relevant"}
SCREEN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["verdict", "confidence"],
    "properties": {"verdict": {"enum": ["relevant", "not_relevant"]}, "confidence": {"type": "number"}},
}
AFFIL_TEMPLATE = "List the byline authors and their organisations.\nJSON only."
AFFIL_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["authors"],
    "properties": {"authors": {"type": "array", "items": {
        "type": "object", "required": ["name", "orgs"],
        "properties": {"name": {"type": "string"}, "orgs": {"type": "array", "items": {"type": "string"}}}}}},
}


def _pin(template: str, schema: Mapping[str, Any], default_name: str):  # type: ignore[no-untyped-def]
    def prompt(pin: Mapping[str, Any], llm: Any) -> PromptPin:
        role = llm.roles[ROLE]
        digests, models = [], []
        for model in [role.model, *role.fallbacks]:
            params: dict[str, Any] = {"model": model, "max_tokens": role.max_tokens}
            if role.temperature is not None:
                params["temperature"] = role.temperature
            digests.append(template_hash(template, schema, params))
            models.append(model)
        return PromptPin(name=pin.get("name", default_name), template_digests=tuple(digests),
                         models=tuple(models))

    return prompt


def user_text(item: AskItem) -> str:
    return f"Title: {item.row['metadata']['title']}\n\n{item.text or ''}"


#: ``doc_id`` → an error ``screen_ask`` raises instead of asking (tests set it with monkeypatch):
#: a failure surfaced by the client (``stack.llm.LlmError``) without the client's own retries.
FAIL: dict[str, Exception] = {}


def screen_ask(item: AskItem, actx: AskContext) -> Answer:
    if item.doc_id in FAIL:
        raise FAIL[item.doc_id]
    res = actx.client.complete(role=ROLE, template=SCREEN_TEMPLATE, refs=SCREEN_REFS, user=user_text(item),
                               schema=SCREEN_SCHEMA, license_class=item.license_class,  # type: ignore[arg-type]
                               prompt_name=actx.pin.name, basis_digest=item.basis_digest)
    p = res.parsed
    return Answer({"verdict": p["verdict"], "confidence": float(p["confidence"]),
                   "basis_role": (item.row.get("content") or {}).get("current_role"),
                   "buckets": [], "quotes": [], "reasoning": "fake"}, llm=res)


def affiliation_ask(item: AskItem, actx: AskContext) -> Answer:
    res = actx.client.complete(role=ROLE, template=AFFIL_TEMPLATE, refs={}, user=user_text(item),
                               schema=AFFIL_SCHEMA, license_class=item.license_class,  # type: ignore[arg-type]
                               prompt_name=actx.pin.name, basis_digest=item.basis_digest)
    items = [{"item_key": str(i), "author_name": a["name"], "author_surface": a["name"], "byline_index": i,
              "affiliations": [{"surface": o, "org_slugs": [o.lower().replace(" ", "-")]} for o in a["orgs"]],
              "orcid": None, "resolution": "resolved" if a["orgs"] else "unresolved"}
             for i, a in enumerate(res.parsed["authors"])]
    return Answer({"items": items}, llm=res, status="ok" if items else "empty")


SCREEN = Workflow("screen", ask=screen_ask, prompt=_pin(SCREEN_TEMPLATE, SCREEN_SCHEMA, "screen-test"))
AFFILIATION = Workflow("affiliation", ask=affiliation_ask,
                       prompt=_pin(AFFIL_TEMPLATE, AFFIL_SCHEMA, "affiliation-test"))


# ---------------------------------------------------------------- a tiny store to annotate
REPO = Path(__file__).resolve().parents[2]
STAGES_ROOT = REPO / "tests" / "fakes" / "annotate_stages"
#: The plan the test runs name: its ``prompts`` are what X6 reads back by name (``dev-v2``).
PLAN_FILE = REPO / "configs" / "plans" / "dev-v2.yaml"
GATHER_BLOCKS = ["identity", "metadata", "inband"]


def plan(screen: dict[str, Any] | None = None, affiliation: dict[str, Any] | None = None) -> Plan:
    """``dev-v2`` (its prompt pins) with the two annotate stage lines replaced. Each line asks one document at a
    time (``options.workers: 1``) unless its ``options`` name ``workers``: the tests built on it pin sequential
    asking, which was the code default until D692 (now 64, tested on its own)."""
    base = load_plan(PLAN_FILE)
    lines = [StageLine.model_validate({"stage": "annotate.screen", "prior": ["screen", "screen_evidence"],
                                       **_sequential(screen)}),
             StageLine.model_validate({"stage": "annotate.affiliation",
                                       "prior": ["affiliation", "affiliation_evidence"], **_sequential(affiliation)})]
    return base.model_copy(update={"stages": lines})


def _sequential(line: dict[str, Any] | None) -> dict[str, Any]:
    """``line`` with ``options.workers: 1`` added when its options do not name ``workers``."""
    line = dict(line or {})
    line["options"] = {"workers": 1, **(line.get("options") or {})}
    return line


def doc_with_text(store: Path, i: int, text: str | None, *, state: str = "acquired") -> dict[str, Any]:
    """``documents`` row ``i`` (synthetic factory) whose current text is ``text``, stored as the
    ``text`` blob of its digest; ``text=None`` with ``state != 'acquired'`` has no content."""
    row = document_row(i, 1, state=state)
    blobs = open_blobs(store)
    for e in row["inband"]["entries"]:  # every reference resolves, with its size (X5)
        body = f"Abstract {i}".encode()
        e["text"] = {"sha256": blobs.put(body), "bytes": len(body), "media_type": "text/plain"}
    if text is not None:
        raw = f"%PDF raw bytes of paper {i}".encode()
        snap = row["content"]["snapshots"][0]
        snap["raw"] = {"sha256": blobs.put(raw), "bytes": len(raw), "media_type": "application/pdf"}
        body = text.encode("utf-8")
        snap["text"] = {"sha256": blobs.put(body), "bytes": len(body), "media_type": "text/markdown"}
        row["content"]["current_text_sha256"] = snap["text"]["sha256"]
    return with_digests(row)


def write_documents(store: Path, rows: list[dict[str, Any]], run: int) -> None:
    """A full ``gather`` file of ``rows`` on the ``documents`` chain, with an ``ok`` run record."""
    write_on_head(store, "documents", "documents", rows, stage="gather", run=run,
                  blocks_written=GATHER_BLOCKS, kind="full")


def revise(store: Path, i: int, text: str, run: int) -> None:
    """An ``acquire`` part on the ``documents`` head that changes document ``i``'s current text."""
    row = doc_with_text(store, i, text)
    row["run_id"] = row["content"]["run_id"] = rid(run)
    write_on_head(store, "documents", "documents", [row], stage="acquire", run=run,
                  blocks_written=["content"], kind="part")


def fold_corpus(store: Path, p: Plan, filter: str | None = None) -> str:
    """Run ``fold.corpus`` through the stage runner from ``documents/main/`` into
    ``corpus/main/``; the corpus file's store path (annotate's exact ``--in``)."""
    rec = run_stage(store, p, "fold.corpus", [sdir("documents")], [sdir("corpus")], filter=filter)
    assert rec.status == "ok", rec.error
    return rec.outputs["corpus"]["path"]


def annotate(store: Path, p: Plan, wf: str, corpus: str, prior: Any = None,
             only: list[str] | None = None) -> RunRecord:
    """Run ``annotate.<wf>`` on the corpus file ``corpus`` (a store path) into ``<wf>/main/`` and
    ``<wf>_evidence/main/``; ``prior`` = an earlier run record, whose exact output files are
    read. ``overrides/main/`` is read when the store has overrides, as ``stack build`` does.
    Without a prior into a non-empty directory the run is a restart (D373)."""
    fams = (wf, f"{wf}_evidence")
    pri = [] if prior is None else [prior.outputs[f]["path"] for f in fams]
    restart = prior is None and any(head_of(store, sdir(f)) for f in fams)
    rec = run_stage(store, p, f"annotate.{wf}", [corpus, *overrides_input(store), *pri], only=only,
                    series=SERIES, restart=restart)
    assert rec.status == "ok", rec.error
    return rec


def rows_of(store: Path, rec: RunRecord, family: str) -> list[dict[str, Any]]:
    return read_family(store / rec.outputs[family]["path"])[0].to_pylist()


def hand_edit(store: Path, rec: RunRecord, family: str, edit: Any) -> None:
    """Rewrite ``rec``'s ``family`` output with ``edit(rows)`` applied (same keys, same header) and
    the run record's ``file_digest`` updated, as a hand edit that kept the store's bookkeeping."""
    path = store / rec.outputs[family]["path"]
    tbl, header = read_family(path)
    rows = tbl.to_pylist()
    edit(rows)
    path.unlink()  # the writer never overwrites
    write_family(path, pa.Table.from_pylist(rows, schema=tbl.schema), header)
    full = find_run_record(store, rec.run_id)
    assert full is not None
    full.outputs[family]["file_digest"] = file_digest(path)
    rewrite_record(store, full)


AT_RUN = _dt.datetime(2026, 10, 12, 12, 0, tzinfo=_dt.UTC)


def write_plan_file(cwd: Path, name: str, *, prompts: dict[str, Any], stages: list[dict[str, Any]],
                    folded: list[str], require_complete: list[str] | None = None) -> Plan:
    """``dev-v2`` renamed ``name`` with ``prompts`` added, ``stages`` and ``annotations_folded``
    replaced, written to ``<cwd>/configs/plans/<name>.yaml`` (X6 finds a plan by the name in the
    file headers, ``stack.build.find_plan``, so the test runs from ``cwd``) and loaded."""
    import yaml

    data = yaml.safe_load(PLAN_FILE.read_text("utf-8"))
    data["plan"] = name
    data["prompts"].update(prompts)
    data["fold"]["build"]["annotations_folded"] = folded
    data["fold"]["diagnostic"] = False
    data["stages"] = stages
    data["require_complete"] = require_complete or []
    path = cwd / "configs" / "plans" / f"{name}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), "utf-8")
    return load_plan(path)
