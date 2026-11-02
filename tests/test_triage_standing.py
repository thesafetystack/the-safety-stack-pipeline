"""Triage arm 7, the carried standing (T5.5; ``stack/gate.py``, ``workflows/triage/main.py``,
X14 in ``stack/checks/triage.py``): an already-acquired document keeps its triage standing when a
later gate does not ask about it (the dev store's 294 rows re-decided ``skipped[not_needed]`` by a
``gate:`` edit, RUNLOG (C) 15:36-16:01).

Pure cases on :func:`stack.gate.decide`; store cases run the real ``gather`` and ``triage``
stages over synthetic sources (``tests/test_triage_questions.py``'s ``acquired_row``: a dev-gate
rule ``fetch``, then an acquire part with the primary role ``ok``), a question gate with no
asker (the ``triage --only select`` path of a ``gate:`` edit, question rows
``skipped[not_requested]``), the chain walk past a newest full file, X14 and ``fold.corpus`` with
``passed_triage``.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from stack import gate
from stack.checks.verify import run_checks
from stack.plan import Plan
from stack.runs import RunRecord
from stack.store.chain import resolve
from stack.store.parquet import read_family
from tests.synth.records import run_line, sdir
from tests.synth.rows import sid
from tests.test_gather_identity import Store, src
from tests.test_gather_merge import write_documents_part
from tests.test_triage_questions import GATE, acquired_row, plan_with
from tests.test_triage_rules import ann, fid, paper

Row = dict[str, Any]
Q = {"question": "safety_relevance", "prompt": "triage.safety-relevance-v1"}


@pytest.fixture
def no_asker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "ASKER_HOOK", lambda plan, line: None)


def triage(store: Store, plan: Plan) -> RunRecord:
    rec = run_line(store.root, plan, "triage")
    assert rec is not None and rec.status == "ok", rec and rec.error
    return rec


def decision(row: Row) -> tuple[Any, ...]:
    a = ann(row)
    return (a["payload"]["decision"], a["status"], a["error_class"], a["origin"], tuple(a["payload"]["wanted_by"]))


def x14(store: Store, plan_path: Path | str) -> tuple[str, list[str]]:
    build = {"plan": str(plan_path), "files": {"documents": {"file_id": fid(store)}}}
    r = run_checks(store.root, names=["gate_union"], build=build)[0]
    return r.status, list(r.all_items)


# ---------------------------------------------------------------- decide (pure)
def standing(decision_: str, wanted_by: list[str], origin: str = "rule") -> Row:
    return {"status": "ok", "origin": origin, "payload": {"decision": decision_, "wanted_by": wanted_by}}


def test_decide_arm_7() -> None:
    cls_ = gate.clauses(Plan.model_validate({"plan": "p", "schema_version": 2, "gate": GATE}).gate)
    fill = {"safety_relevance": {"status": "skipped", "error_class": "not_needed"}}
    assert gate.decide(cls_, (), fill).arm == 6
    d = gate.decide(cls_, (), fill, standing=standing("fetch", ["screen", "gone"]))
    assert (d.decision, d.status, d.error_class, d.wanted_by, d.origin, d.arm) == ("fetch", "ok", None, ("screen",), "rule", 7)
    d = gate.decide(cls_, (), {}, standing=standing("skip", [], "ai_check"))
    assert (d.decision, d.status, d.wanted_by, d.origin, d.arm) == ("skip", "ok", (), "ai_check", 7)
    # a fetch whose wanted_by has none of the gate's consumers is not carried; nor an undecided standing
    assert gate.decide(cls_, (), fill, standing=standing("fetch", ["gone"])).arm == 6
    pending = {"status": "skipped", "error_class": "not_needed", "payload": {"decision": "pending", "wanted_by": []},
               "origin": "rule"}
    assert gate.decide(cls_, (), fill, standing=pending).arm == 6
    # arms 1-5 come first; a question row that is not a re-derived fill is not arm 6's carry
    ok_neg = {"safety_relevance": {"status": "ok", "error_class": None, "verdict": "not_relevant"}}
    assert gate.decide(cls_, (), ok_neg, standing=standing("fetch", ["screen"])).arm == 2
    nb = {"safety_relevance": {"status": "skipped", "error_class": "no_basis"}}
    assert gate.decide(cls_, (), nb, standing=standing("fetch", ["screen"])).arm == 3


def test_merged_standing_fetch_wins() -> None:
    """D263 (R3-4): any decided ``fetch`` among the document's own and its former ids'
    standings wins, ``wanted_by`` united; else the first ``skip``; undecided ones are ignored."""
    pending = {"status": "skipped", "origin": "rule", "payload": {"decision": "pending", "wanted_by": []}}
    skip = standing("skip", [], "ai_check")
    got = gate.merged_standing(skip, [standing("fetch", ["screen"]), pending, standing("fetch", ["review"], "x")])
    assert (got["payload"]["decision"], got["payload"]["wanted_by"], got["origin"]) == ("fetch", ["review", "screen"], "rule")
    assert gate.merged_standing(standing("fetch", ["review"], "own"), [standing("fetch", ["screen"])])["origin"] == "own"
    assert gate.merged_standing(None, [pending, skip]) == skip
    assert gate.merged_standing(pending, [None]) is None


# ---------------------------------------------------------------- the stage (store)
@pytest.mark.slow
def test_merge_carries_the_absorbed_fetch_over_the_survivors_skip(tmp_path: Path, no_asker: None) -> None:
    """R3-4 (D263): openalex ``sid(2)`` was decided ``skip``; arXiv ``sid(7)`` was rule-``fetch``
    and acquired; a new ``sid(2)`` row adds ``sid(7)``'s arXiv key, so gather merges ``sid(7)``
    into the earlier ``sid(2)`` with its text. The merged document stands on ``fetch`` (arm 7),
    and the next run does not churn."""
    store = Store(tmp_path / "store")
    store.add("sources.openalex", [src(2, [("openalex", "W2"), ("doi", "10.9/b")], t=1, channel="openalex")])
    store.gather()
    triage(store, store.plan)
    row = copy.deepcopy(store.docs()[sid(2)])
    t = row["annotations"]["triage"]
    t.update(status="ok", error_class=None, origin="ai_check")
    t["payload"].update(decision="skip", wanted_by=[])
    write_documents_part(store, [row], "triage", ["annotations"])
    acquired_row(store, 7)
    store.add("sources.openalex", [src(2, [("openalex", "W2"), ("doi", "10.9/b"), ("arxiv", "2610.00007")],
                                       t=60, channel="openalex")])
    store.gather()
    merged = store.docs()[sid(2)]
    assert merged["identity"]["former_doc_ids"] == [sid(7)] and decision(merged)[0] == "skip"
    plan_q, path_q = plan_with(tmp_path, "q", GATE)
    rec = triage(store, plan_q)
    assert decision(store.docs()[sid(2)]) == ("fetch", "ok", None, "rule", ("screen",))
    assert rec.counts["arm7"] == 1
    assert x14(store, path_q) == ("ok", [])
    rec2 = triage(store, plan_q)
    assert rec2.counts["scoped"] == 0 and "documents" not in rec2.outputs  # S25-52: no empty part
    assert decision(store.docs()[sid(2)]) == ("fetch", "ok", None, "rule", ("screen",))


@pytest.mark.slow
def test_gate_edit_carries_acquired_rows_and_x14_fold_agree(tmp_path: Path, no_asker: None) -> None:
    store = Store(tmp_path / "store")
    acquired_row(store, 7)                       # (a) rule fetch, then full_text ok
    store.add("sources.arxiv_sweep", [paper(1)])  # (c) rule fetch, not acquired
    store.gather()
    triage(store, store.plan)
    assert decision(store.docs()[sid(1)]) == ("fetch", "ok", None, "rule", ("screen",))
    plan_q, path_q = plan_with(tmp_path, "q", GATE)
    rec = triage(store, plan_q)
    docs = store.docs()
    # (a) arm 7: the standing's fetch, wanted_by filtered to {screen, review}, origin rule
    assert decision(docs[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",))
    a7 = ann(docs[sid(7)])
    assert [(q["status"], q["error_class"]) for q in a7["payload"]["questions"]] == [("skipped", "not_requested")]
    assert a7["config_digest"] == rec.config_digest and a7["basis_digest"] == docs[sid(7)]["metadata"]["fields_digest"]
    assert docs[sid(7)]["state"] == "acquired"
    # (c) primary role not ok: arm 6 as before, no carry
    assert decision(docs[sid(1)]) == ("pending", "skipped", "not_needed", "rule", ())
    assert (rec.counts["arm7"], rec.counts["arm6"], rec.counts["standing_due"]) == (1, 1, 0)
    # (d) X14 agrees with arm 7 ...
    assert x14(store, path_q) == ("ok", [])
    # (e) passed_triage keeps the carried row (it dropped it before T5.5)
    out = run_line(store.root, plan_q, "fold.corpus", filter="passed_triage")
    assert out is not None and out.status == "ok", out and out.error
    corpus, _ = read_family(store.root / out.outputs["corpus"]["path"])
    assert corpus.column("doc_id").to_pylist() == [sid(7)]
    # (d) ... and is red on a primary-role-not-ok row hand-edited to fetch under arm 6's conditions
    row = copy.deepcopy(store.docs()[sid(1)])
    t = row["annotations"]["triage"]
    t.update(status="ok", error_class=None)
    t["payload"].update(decision="fetch", wanted_by=["screen"])
    write_documents_part(store, [row], "triage", ["annotations"])
    status, items = x14(store, path_q)
    assert status == "failed" and len(items) == 1 and sid(1) in items[0] and "'fetch'" in items[0]
    # a gate whose consumers do not include the standing's: the fetch is not carried (arm 6)
    plan_r, _ = plan_with(tmp_path, "r", {"review": [{**Q, "fetch_on": ["relevant"]}]})
    rec_r = triage(store, plan_r)
    assert decision(store.docs()[sid(7)]) == ("pending", "skipped", "not_needed", "rule", ())
    assert rec_r.counts["arm7"] == 0


@pytest.mark.slow
def test_repair_of_a_row_already_not_needed(tmp_path: Path, no_asker: None) -> None:
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    plan_q, path_q = plan_with(tmp_path, "q", GATE)
    # what the pre-T5.5 gate edit left: sid(7) pending, skipped[not_needed] under the current config
    row = copy.deepcopy(store.docs()[sid(7)])
    cfg = gate.triage_config_digest(plan_q.stage_line("triage"), plan_q.gate, None)
    q = gate.questions(plan_q)[0]
    row["annotations"]["triage"] = gate.annotation(
        gate.Decision("pending", "skipped", "not_needed", (), "rule", 6), [gate.question_row(q, "skipped", "not_needed")],
        basis_digest=row["metadata"]["fields_digest"], config_digest=cfg, decided_at=row["updated_at"])
    write_documents_part(store, [row], "triage", ["annotations"])
    # a gather full file on top: the decided fetch is only in files before the newest full file
    store.gather()
    ch = resolve(store.root, sdir("documents"))
    assert len(ch.files) == 1 and ch.files[0].is_full
    assert decision(store.docs()[sid(7)]) == ("pending", "skipped", "not_needed", "rule", ())
    assert not gate.in_scope(store.docs()[sid(7)], cfg)
    found = gate.standings(store.root, ch, [sid(7)])
    assert found[sid(7)]["payload"]["decision"] == "fetch"
    # the next triage run, no gate edit: back to fetch, in scope by its standing alone
    rec = triage(store, plan_q)
    assert rec.config_digest == cfg
    assert (rec.counts["scoped"], rec.counts["standing_due"], rec.counts["arm7"]) == (1, 1, 1)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",))
    assert x14(store, path_q) == ("ok", [])
    # no churn: a further run scopes nothing
    rec2 = triage(store, plan_q)
    assert (rec2.counts["scoped"], rec2.counts["standing_due"]) == (0, 0)
    assert "documents" not in rec2.outputs  # S25-52: no empty part


@pytest.mark.slow
def test_no_standing_no_scope(tmp_path: Path, no_asker: None) -> None:
    """A not_needed acquired row with no decided triage anywhere in the chain is not added."""
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    plan_q, _ = plan_with(tmp_path, "q", GATE)
    docs = store.docs()
    row = copy.deepcopy(docs[sid(7)])
    cfg = gate.triage_config_digest(plan_q.stage_line("triage"), plan_q.gate, None)
    q = gate.questions(plan_q)[0]
    row["annotations"]["triage"] = gate.annotation(
        gate.Decision("pending", "skipped", "not_needed", (), "rule", 6), [gate.question_row(q, "skipped", "not_needed")],
        basis_digest=row["metadata"]["fields_digest"], config_digest=cfg, decided_at=row["updated_at"])
    write_documents_part(store, [row], "triage", ["annotations"])
    store.gather()  # a full file on top: the decided triage is only in the files before it
    ch = resolve(store.root, sdir("documents"))
    assert gate.standings(store.root, ch, [sid(7)])  # still in the walk
    walk = gate.chain_walk(store.root, ch)
    assert walk[0] == ch.files[0].path and len(walk) > 1
    for p in walk[1:]:
        p.unlink()  # the earlier files are gone (gc): nothing to stand on
    assert gate.chain_walk(store.root, ch) == walk[:1] and not gate.standings(store.root, ch, [sid(7)])
    rec = triage(store, plan_q)
    assert (rec.counts["scoped"], rec.counts["standing_due"]) == (0, 0)


def html_only_row(store: Store, i: int) -> None:
    """:func:`tests.test_triage_questions.acquired_row`, but acquired as ``arxiv_pdf: on_miss`` leaves
    an arXiv paper whose HTML is the text: ``html`` ``ok`` and current, ``full_text``
    ``skipped[not_requested]`` (:func:`tests.synth.rows.html_only`)."""
    from tests.synth.rows import _content, html_only
    from tests.test_gather_merge import write_documents_part as write_part

    store.add("sources.arxiv_sweep", [paper(i)])
    store.gather()
    rec = run_line(store.root, store.plan, "triage")
    assert rec is not None and rec.status == "ok" and ann(store.docs()[sid(i)])["payload"]["decision"] == "fetch"
    row = copy.deepcopy(store.docs()[sid(i)])
    content = html_only(_content(i, 600))
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_part(store, [row], "acquire", ["content"])


def with_arxiv_pdf(path: Path, mode: str | None) -> tuple[Plan, Path]:
    """The plan file at ``path`` with its acquire line's ``options.arxiv_pdf`` set to ``mode`` (and the
    arXiv HTML on, which ``on_miss``/``never`` need), written back."""
    import yaml

    from stack.plan import load_plan

    data = yaml.safe_load(path.read_text())
    for line in data["stages"]:
        if line["stage"] == "acquire":
            opts = line.setdefault("options", {}) or {}
            opts["structured"] = {"arxiv_html": True}
            if mode is not None:
                opts["arxiv_pdf"] = mode
            line["options"] = opts
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return load_plan(path), path


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["on_miss", "never", None])
def test_html_only_paper_is_not_re_asked_after_a_gate_edit(tmp_path: Path, no_asker: None, mode: str | None) -> None:
    """D485: an arXiv paper acquired HTML-only (its ``full_text`` ``skipped[not_requested]``, its ``html``
    ``ok``) under a plan whose acquire line sets ``arxiv_pdf: on_miss`` or ``never``: a ``gate:`` edit
    does not put it in the question scope (no re-ask), it keeps its ``fetch`` by arm 7, and X14 agrees.
    Without the option (``always``, the old rule: the primary role is the PDF) the same row is in the
    question scope and falls to arm 6, which is why the option decides it."""
    store = Store(tmp_path / "store")
    html_only_row(store, 7)
    plan_q, path_q = with_arxiv_pdf(plan_with(tmp_path, "q", GATE)[1], mode)
    assert gate.html_is_primary(plan_q) is (mode is not None)
    rec = triage(store, plan_q)
    row = store.docs()[sid(7)]
    assert row["content"]["current_role"] == "html" and row["state"] == "acquired"
    if mode is not None:
        assert decision(row) == ("fetch", "ok", None, "rule", ("screen",))
        assert (rec.counts["question_scope"], rec.counts["arm7"]) == (0, 1)
    else:
        assert decision(row) == ("pending", "skipped", "not_needed", "rule", ())
        assert (rec.counts["question_scope"], rec.counts["arm7"]) == (1, 0)
    assert x14(store, path_q) == ("ok", [])


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["on_miss", "never", None])
def test_pdf_set_aside_after_triage_keeps_arm_7(tmp_path: Path, no_asker: None, mode: str | None) -> None:
    """VERIFY-2A (build wjcmrvtam57r8, doc shcnz0x6q1ajb): triage carries an acquired paper's ``fetch``
    by arm 7 (its PDF ``full_text`` ``ok``); acquire then sets the PDF aside under ``arxiv_pdf: on_miss``
    (``skipped[not_requested]``, "waiting for the arXiv HTML": a merged member's ``html`` role is not
    fetched yet; D480 skips whatever the status, snapshot kept). The PDF is still held, so the row stays
    primary-``ok`` and X14 recomputes arm 7. Under ``always`` the rule never sets a PDF aside; a
    hand-made one is primary there too since D704 (a held snapshot counts for any primary role; red
    under D621's ``on_miss | never``-only rule)."""
    store = Store(tmp_path / "store")
    acquired_row(store, 7)
    plan_q, path_q = with_arxiv_pdf(plan_with(tmp_path, "q", GATE)[1], mode)
    rec = triage(store, plan_q)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",)) and rec.counts["arm7"] == 1
    row = copy.deepcopy(store.docs()[sid(7)])
    (pdf,) = row["content"]["roles"]
    pdf.update(status="skipped", error_class="not_requested", error="arxiv_pdf: on_miss, waiting for the arXiv HTML",
               retry_after=None, rule="arxiv_pdf")
    html = {**pdf, "content_role": "html", "status": "not_attempted", "error_class": "limit", "error": "not yet fetched",
            "attempts": 0, "first_attempt_at": None, "last_attempt_at": None, "last_outcome": None, "tried_urls": [],
            "snapshot_id": None, "rule": None}
    row["content"]["roles"] = [pdf, html]
    write_documents_part(store, [row], "acquire", ["content"])
    assert x14(store, path_q) == ("ok", [])
    wants = plan_q.acquire.wants

    def primary(*roles: Row) -> bool:
        return gate.primary_role_ok({**row, "content": {**row["content"], "roles": list(roles)}}, wants,
                                    html_primary=True)

    # the next state: the HTML ended empty, the PDF is requested again and the one-fetch rule lets another
    # member's role fetch it; the held PDF's role reads skipped[not_needed] meanwhile
    covered = {**pdf, "error_class": "not_needed", "error": "same arxiv:7 as other (openalex)"}
    assert primary(covered, {**html, "status": "empty", "error_class": None})
    # a set-aside role that never fetched (an HTML-only paper's PDF) is not primary, nor is a dangling pointer
    assert not primary({**pdf, "last_outcome": None, "snapshot_id": None}, html)
    assert not primary({**pdf, "snapshot_id": "gone.0"}, html)


@pytest.mark.slow
@pytest.mark.parametrize("mode", ["on_miss", "never"])
@pytest.mark.parametrize("outcome", ["withdrawn", "empty"])
def test_html_recheck_after_triage_keeps_arm_7(tmp_path: Path, no_asker: None, mode: str, outcome: str) -> None:
    """D704 (build 2a's gate_union, docs shtc / smwe): triage carries an HTML-only arXiv paper's ``fetch``
    by arm 7 (its ``html`` ``ok``, primary under ``on_miss``/``never``); acquire's weekly recheck of that
    role, later in the same build, answers ``skipped[withdrawn]`` or ``empty`` (``no_html:http_404``) and
    keeps the role's ``snapshot_id``. The fetched HTML is still held, so the row stays primary-``ok``
    (D704's held-snapshot rule): X14 recomputes arm 7, and a later ``gate:`` edit
    carries ``fetch`` again instead of putting the row in the question scope (arm 6 without an asker)."""
    from stack.fetchcycle import role_settled, role_skipped
    from tests.synth.rows import AT

    store = Store(tmp_path / "store")
    html_only_row(store, 7)
    plan_q, path_q = with_arxiv_pdf(plan_with(tmp_path, "q", GATE)[1], mode)
    rec = triage(store, plan_q)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",)) and rec.counts["arm7"] == 1
    row = copy.deepcopy(store.docs()[sid(7)])
    roles = row["content"]["roles"]
    i = next(k for k, r in enumerate(roles) if r["content_role"] == "html")
    held = roles[i]["snapshot_id"]
    assert held is not None and roles[i]["status"] == "ok"
    if outcome == "withdrawn":
        roles[i] = role_skipped(roles[i], AT, error_class="withdrawn", error="arXiv: this version has been withdrawn")
    else:
        roles[i] = {**role_settled(roles[i], AT, status="empty", outcome="empty"), "last_outcome": "no_html:http_404"}
    assert roles[i]["snapshot_id"] == held  # acquire's _apply keeps the pointer when nothing is minted
    write_documents_part(store, [row], "acquire", ["content"])
    assert x14(store, path_q) == ("ok", [])
    row = store.docs()[sid(7)]
    wants = plan_q.acquire.wants
    assert gate.primary_role_ok(row, wants, html_primary=True)
    assert not gate.in_question_scope(row, False, wants, html_primary=True)

    # a later gate edit re-decides the row: still outside the question scope, fetch carried by arm 7
    plan_r, path_r = with_arxiv_pdf(plan_with(tmp_path, "r", {**GATE, "review": [{**Q, "fetch_on": ["relevant"]}]})[1],
                                     mode)
    rec = triage(store, plan_r)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",))
    assert (rec.counts["question_scope"], rec.counts["arm7"]) == (0, 1)
    assert x14(store, path_r) == ("ok", [])

    # the rule needs the pointer to be a held raw snapshot: a dangling or absent pointer is not primary
    for sid_ in (None, "gone.0"):
        bare = copy.deepcopy(row)
        bare["content"]["roles"][i]["snapshot_id"] = sid_
        assert not gate.primary_role_ok(bare, wants, html_primary=True)


@pytest.mark.slow
@pytest.mark.parametrize(("channel", "chain", "key", "role"), [
    ("openai_blog", "sources.blogs", ("url", "https://openai.com/index/p7"), "page"),
    ("lesswrong", "sources.lesswrong", ("lesswrong", "post7"), "post"),
])
def test_failed_recheck_of_a_web_role_after_triage_keeps_arm_7(tmp_path: Path, no_asker: None, channel: str,
                                                                 chain: str, key: tuple[str, str], role: str) -> None:
    """D704 for a primary role that is not arXiv's (the head's 1,152 openai.com pages, ``page``
    ``failed_permanent[blocked]``: HTTP 403 at the fetch cap): a row acquired over ``http`` (its primary
    ``page`` / ``post`` role ``ok``) keeps its ``fetch`` by arm 7 under a question gate; the recheck
    then fails later in the same build, the role's ``snapshot_id`` kept. X14 recomputes arm 7, and a
    later ``gate:`` edit carries ``fetch`` again. D621's rule held only a ``full_text`` role under
    ``on_miss | never``, so both were red (arm 6) before D704."""
    from stack.fetchcycle import role_failed
    from tests.synth.rows import AT, _content

    store = Store(tmp_path / "store")
    store.add(chain, [src(7, [key], channel=channel)])
    store.gather()
    triage(store, store.plan)  # the dev gate's sql: fetch
    assert ann(store.docs()[sid(7)])["payload"]["decision"] == "fetch"
    row = copy.deepcopy(store.docs()[sid(7)])
    content = _content(7, 600)
    (snap,), (r0,) = content["snapshots"], content["roles"]
    url = key[1] if key[0] == "url" else f"https://www.lesswrong.com/posts/{key[1]}"
    snap.update(content_role=role, url=url, raw={**snap["raw"], "media_type": "text/html"}, revision=None)
    r0.update(content_role=role, lane="http", tried_urls=[url])
    content.update(current_role=role, current_revision=None, revisions=[])
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_documents_part(store, [row], "acquire", ["content"])
    plan_q, path_q = plan_with(tmp_path, "q", GATE)
    assert not gate.html_is_primary(plan_q) and plan_q.acquire.wants[channel] == [role]
    rec = triage(store, plan_q)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",)) and rec.counts["arm7"] == 1

    row = copy.deepcopy(store.docs()[sid(7)])
    (r0,) = row["content"]["roles"]
    row["content"]["roles"] = [role_failed(r0, AT, status="failed_permanent", error_class="blocked", error="http_403")]
    assert row["content"]["roles"][0]["snapshot_id"] == snap["snapshot_id"]
    write_documents_part(store, [row], "acquire", ["content"])
    assert x14(store, path_q) == ("ok", [])
    wants = plan_q.acquire.wants
    assert gate.primary_role_ok(store.docs()[sid(7)], wants)

    plan_r, path_r = plan_with(tmp_path, "r", {**GATE, "review": [{**Q, "fetch_on": ["relevant"]}]})
    rec = triage(store, plan_r)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "rule", ("screen",))
    assert (rec.counts["question_scope"], rec.counts["arm7"]) == (0, 1)
    assert x14(store, path_r) == ("ok", [])

    for sid_ in (None, "gone.0"):  # no held snapshot: as before D704, the failed role is not primary
        bare = copy.deepcopy(store.docs()[sid(7)])
        bare["content"]["roles"][0]["snapshot_id"] = sid_
        assert not gate.primary_role_ok(bare, wants)


@pytest.mark.slow
def test_merge_with_an_unfetched_html_keeps_the_held_pdf_and_arm_7(tmp_path: Path, no_asker: None,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """VERIFY-2A's trigger end to end (D643, amends D480): a Semantic Scholar paper on an arXiv key is
    triaged ``fetch`` and acquired PDF-only (its arXiv ``html`` answered 404: ``empty``, settled; the
    PDF ``ok``, held). An OpenAlex work on the same arXiv id merges in; triage (``arxiv_pdf: on_miss``)
    carries ``fetch`` by arm 7, since the PDF is ``ok``. Acquire's first run then reaches the row with
    carry alone (unreached, D587): the new member wants its own arXiv ``html`` (``not_attempted``, and
    OpenAlex leads the one-fetch group), so the id's HTML is ``pending`` again. The held PDF stays ``ok``
    (before D643 it was set aside, ``skipped[not_requested]``), and X14 recomputes arm 7, without
    the held-snapshot rule (D621, D704) too. After the new ``html`` ends ``empty`` as well, the PDF role is still ``ok``."""
    import datetime as dt

    from tests.synth.rows import AT, _content
    from tests.test_acquire_core import GROUPS
    from workflows.acquire.carry import carry
    from workflows.acquire.scope import FETCH

    aid = "2610.00007"
    store = Store(tmp_path / "store")
    store.add("sources.semantic_scholar", [src(7, [("arxiv", aid)], channel="semantic_scholar")])
    store.gather()
    triage(store, store.plan)
    row = copy.deepcopy(store.docs()[sid(7)])
    t = row["annotations"]["triage"]
    t.update(status="ok", error_class=None, origin="ai_check")
    t["payload"].update(decision="fetch", wanted_by=["screen"])
    write_documents_part(store, [row], "triage", ["annotations"])
    row = copy.deepcopy(store.docs()[sid(7)])
    content = _content(7, 600)
    (pdf,) = content["roles"]
    html = {**pdf, "content_role": "html", "status": "empty", "error_class": None, "error": "no_html:http_404",
            "last_outcome": "empty", "snapshot_id": None, "tried_urls": [f"https://arxiv.org/html/{aid}"],
            "retry_after": AT + dt.timedelta(days=180)}
    content["roles"] = [pdf, html]
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_documents_part(store, [row], "acquire", ["content"])

    store.add("sources.openalex", [src(2, [("doi", f"10.48550/arxiv.{aid}"), ("arxiv", aid)], t=60,
                                       channel="openalex")])
    store.gather()
    merged = store.docs()[sid(7)]
    assert sorted(m["source_id"] for m in merged["identity"]["members"]) == [sid(2), sid(7)]
    plan_q, path_q = with_arxiv_pdf(plan_with(tmp_path, "q", GATE)[1], "on_miss")
    rec = triage(store, plan_q)
    assert decision(store.docs()[sid(7)]) == ("fetch", "ok", None, "ai_check", ("screen",))
    assert rec.counts["arm7"] == 1

    def acquire_carry(row_: Row) -> tuple[Row, dict[str, int]]:
        counts: dict[str, int] = {}
        out = copy.deepcopy(row_)
        out["content"] = carry(out, {out["doc_id"]: out}, wants=plan_q.acquire.wants, channel_group=GROUPS.get,
                               permit=FETCH, pin=out["content"]["convert_pin"], counts=counts, arxiv_html=True,
                               arxiv_pdf="on_miss")
        return out, counts

    def role_states(row_: Row) -> dict[tuple[str, str], tuple[str, str | None]]:
        return {(r["source_id"], r["content_role"]): (r["status"], r["error_class"]) for r in row_["content"]["roles"]}

    run1, counts = acquire_carry(store.docs()[sid(7)])
    assert role_states(run1) == {(sid(7), "full_text"): ("ok", None), (sid(7), "html"): ("empty", None),
                                 (sid(2), "html"): ("not_attempted", "limit"),
                                 (sid(2), "full_text"): ("skipped", "not_requested"),
                                 (sid(2), "xml"): ("skipped", "not_needed")}  # the incident's sk9 roles
    assert counts["arxiv_pdf.kept_pending"] == 1
    write_documents_part(store, [run1], "acquire", ["content"])
    assert store.docs()[sid(7)]["state"] == "triaged"  # a re-point waits for a run that reaches the row
    assert x14(store, path_q) == ("ok", [])
    with monkeypatch.context() as m:  # the held-snapshot rule (D704) is not what keeps arm 7 here
        m.setattr(gate, "_held_snapshot", lambda content, r: False)
        assert x14(store, path_q) == ("ok", [])

    # the next run fetches the new member's html: the same 404; a miss requests the PDF, the held one stays
    row2 = copy.deepcopy(store.docs()[sid(7)])
    for r in row2["content"]["roles"]:
        if (r["source_id"], r["content_role"]) == (sid(2), "html"):
            r.update(status="empty", error_class=None, error="no_html:http_404", attempts=1)
    run2, _ = acquire_carry(row2)
    assert role_states(run2)[(sid(7), "full_text")] == ("ok", None)
    write_documents_part(store, [run2], "acquire", ["content"])
    assert x14(store, path_q) == ("ok", [])


# ---------------------------------------------------------------- a member moved out (D785)
def _moved_out_world(tmp_path: Path) -> tuple[Store, Plan, Path, Row]:
    """The 2026-11-01 ``gate_union`` halt (doc s9q5): an OpenAlex work ``sid(2)`` (its ``full_text``
    ``failed_permanent``, no snapshot) joined with an arXiv paper ``sid(7)`` whose ``html`` is ``ok``
    (primary under ``arxiv_pdf: on_miss``), decided ``fetch`` with its question a *signal*
    ``relevant`` (asked while outside the question scope, D724). Then the shared arXiv key leaves
    ``sid(2)``'s record and gather moves ``sid(7)`` out: ``sid(2)``'s row is ``identified`` and keeps
    ``sid(7)``'s roles until acquire regroups (``workflows/gather/carry.py``). The gate is
    ``screen``'s question alone, as revision 11's. Returns the store, the question plan and its path,
    and the question's signal row."""
    from tests.synth.rows import AT, _content, dg, eid, html_only

    aid = "2610.00007"
    store = Store(tmp_path / "store")
    store.add("sources.openalex", [src(2, [("openalex", "W2"), ("doi", "10.9/b"), ("arxiv", aid)], t=1,
                                       channel="openalex")])
    store.add("sources.arxiv_sweep", [src(7, [("arxiv", aid)], t=5)])
    store.gather()
    joined = store.docs()[sid(2)]
    assert sorted(joined["identity"]["member_ids"]) == [sid(2), sid(7)]
    assert joined["identity"]["anchor_source_id"] == sid(2) and sid(7) not in store.docs()
    plan_q, path_q = with_arxiv_pdf(plan_with(tmp_path, "q", {"screen": GATE["screen"]})[1], "on_miss")
    (q,) = gate.questions(plan_q)
    signal = {"question": q.name, "status": "skipped", "error_class": "not_needed", "verdict": "relevant",
              "confidence": 0.93, "evidence_id": eid(1), "config_digest": q.config_digest}
    row = copy.deepcopy(joined)
    t = gate.annotation(gate.Decision("fetch", "ok", None, ("screen",), "ai_check", 7), [signal],
                        basis_digest=row["metadata"]["fields_digest"], config_digest=dg("triage-c0"), decided_at=AT)
    row["annotations"] = {**row["annotations"], "triage": t}
    row["state"] = "triaged"
    write_documents_part(store, [row], "triage", ["annotations"])
    row = copy.deepcopy(store.docs()[sid(2)])
    content = html_only(_content(7, 600))
    oa = {**content["roles"][0], "source_id": sid(2), "lane": "oa", "rule": None, "status": "failed_permanent",
          "error_class": "no_fetchable_url", "error": "no full_text artifact", "attempts": 2,
          "first_attempt_at": AT, "last_attempt_at": AT, "last_outcome": "failed_permanent"}
    content["roles"] = [oa, *content["roles"]]
    content["members_digest"] = row["identity"]["members_digest"]
    row["content"], row["state"] = content, "acquired"
    write_documents_part(store, [row], "acquire", ["content"])
    assert x14(store, path_q) == ("ok", [])  # arm 7: sid(7)'s html is a member's primary role
    store.add("sources.openalex", [src(2, [("openalex", "W2"), ("doi", "10.9/b")], t=60, channel="openalex")])
    store.gather()
    moved = store.docs()[sid(2)]
    assert moved["state"] == "identified" and moved["identity"]["member_ids"] == [sid(2)]
    assert sid(7) in {r["source_id"] for r in moved["content"]["roles"]}  # kept until acquire regroups
    assert ann(moved)["basis_digest"] == moved["metadata"]["fields_digest"]  # the signal carries
    return store, plan_q, path_q, signal


def _regroup(store: Store, plan: Plan) -> Row:
    """Acquire's ``carry`` over ``sid(2)``'s row (the regroup that drops a departed member's roles),
    written as an acquire part; the regrouped row."""
    from tests.test_acquire_core import GROUPS
    from workflows.acquire.carry import carry
    from workflows.acquire.scope import FETCH

    row = copy.deepcopy(store.docs()[sid(2)])
    row["content"] = carry(row, {row["doc_id"]: row}, wants=plan.acquire.wants, channel_group=GROUPS.get,
                           permit=FETCH, pin=row["content"]["convert_pin"], arxiv_html=True, arxiv_pdf="on_miss")
    assert {r["source_id"] for r in row["content"]["roles"]} == {sid(2)}
    write_documents_part(store, [row], "acquire", ["content"])
    return store.docs()[sid(2)]


@pytest.mark.slow
def test_a_moved_out_members_roles_do_not_make_the_primary_role_ok(tmp_path: Path, no_asker: None) -> None:
    """D785 (data path): triage between the gather that moved ``sid(7)`` out and acquire's regroup
    must not stand on ``sid(7)``'s ``html``. The row is in the question scope; its carried signal is
    the ``ok`` answer that decides (arm 1, ``ai_check``), stored as an answer, so after the regroup
    X14 agrees. Before D785 triage carried ``fetch`` by arm 7 with the signal still a fill, and X14
    after the regroup recomputed arm 6 (``pending``, ``skipped[not_needed]``)."""
    store, plan_q, path_q, _signal = _moved_out_world(tmp_path)
    moved = store.docs()[sid(2)]
    rec = triage(store, plan_q)
    row = store.docs()[sid(2)]
    assert decision(row) == ("fetch", "ok", None, "ai_check", ("screen",))
    _regroup(store, plan_q)
    assert x14(store, path_q) == ("ok", [])  # the halt's finding before D785
    assert (rec.counts["question_scope"], rec.counts["arm1"], rec.counts["arm7"]) == (1, 1, 0)
    (qrow,) = ann(row)["payload"]["questions"]
    assert (qrow["status"], qrow["verdict"]) == ("ok", "relevant")
    wants = plan_q.acquire.wants
    assert not gate.primary_role_ok(moved, wants, html_primary=True)
    stale = copy.deepcopy(moved)  # the same roles with sid(7) still a member: primary ok (D485)
    stale["identity"]["member_ids"] = [sid(2), sid(7)]
    assert gate.primary_role_ok(stale, wants, html_primary=True)


@pytest.mark.slow
def test_x14_recomputes_a_stored_signal_in_the_question_scope_as_an_answer(tmp_path: Path) -> None:
    """D785 (the check): the row the 2026-11-01 store holds, triage's arm-7 ``fetch`` with a
    ``relevant`` signal, regrouped so its primary role is not ``ok``. Triage would now carry the signal
    as an answer (arm 1, ``fetch``); X14 recomputes the same and agrees (before D785: arm 6, the
    build's one hard finding). A ``not_relevant`` signal recomputes to ``skip`` and a fill without a
    verdict to arm 6: both still disagree with the stored ``fetch``."""
    store, plan_q, path_q, signal = _moved_out_world(tmp_path)
    row = _regroup(store, plan_q)  # identified: the triage parts below decide it (old triage: arm 7, a signal)
    assert not gate.primary_role_ok(row, plan_q.acquire.wants, html_primary=True)
    for sig, want in ((signal, ("ok", [])),
                      ({**signal, "verdict": "not_relevant"}, "skip"),
                      ({**signal, "verdict": None, "confidence": None}, "not_needed")):
        r = copy.deepcopy(store.docs()[sid(2)])
        r["annotations"]["triage"]["payload"]["questions"] = [sig]
        r["state"] = "triaged"
        write_documents_part(store, [r], "triage", ["annotations"])
        status, items = x14(store, path_q)
        if isinstance(want, tuple):
            assert (status, items) == want
        else:
            assert status == "failed" and len(items) == 1 and want in items[0], items
