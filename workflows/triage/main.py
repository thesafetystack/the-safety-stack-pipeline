"""Stage ``triage`` (§3.1, §3.3; D3, D14, D15, D19, D32, D37): the gate over the ``documents``
head, written as a ``documents`` **part** of the rows it touched (``blocks_written =
['annotations']``) and a ``triage_evidence`` file (a chain of one, D14; cumulative, D129).

``stack run triage --in data/documents/<series>/ [--in data/triage_evidence/<series>/] [--only select]``.

Everything the stage decides is :mod:`stack.gate` (gate view, ``sql:`` clauses, the seven-arm
table, ``wanted_by``, scope, carried answers, the asker protocol); this module wires it:

* scope = :func:`stack.gate.in_scope` over the head view (unmasked, not ``retired``, and
  ``identified`` or the triage basis/config stale) **∪ questions due** (§3.3 "scope = due
  chain-view keys ∪ filtered keys", as acquire's "roles due"; D161 (C)): when an asker will run
  (``ask`` not excluded by ``--only``), a current ``pending`` row with a carried question that is
  ``not_attempted`` (budget, halted, limit, unanswered: never asked or never answered) or ``failed_transient`` whose prior
  evidence is due or missing (:func:`stack.gate.question_due`; the annotate retry rule, so scope
  and asker agree; missing evidence, R2-1: asked again as a first attempt) **∪ standing due**
  (T5.5): a ``pending``, ``skipped[not_needed]`` row whose primary role
  is ``ok`` (an ``ok`` arXiv ``html`` role counts for ``full_text`` under the acquire line's
  ``arxiv_pdf: on_miss | never``, :func:`stack.gate.html_is_primary`, D485; a role of any status
  that still points at a held raw snapshot counts too, D704) and that has a decided triage annotation in the pinned chain
  (:func:`stack.gate.standing_due`, :func:`stack.gate.standings`; count ``standing_due``) **∪
  answers due** (D724): when an asker will run, a row whose triage annotation (on its current basis)
  lacks a settled answer for some question, whatever its question scope (:func:`stack.gate.answer_due`:
  no row of the current question digest, or a fill that is not a settled signal; count
  ``answers_due``); rows out of scope are not in the part; a part with no row is not written (S25-52: an empty part
  only counted toward ``max_partials``), unless the run writes the full the part makes (no
  ``--part``);
* the gate's canonical JSON goes to the store's blobs (:func:`stack.gate.store_gate`, X14);
* ``config_digest`` = :func:`stack.gate.triage_config_digest` (stage line, ``gate_digest``, the
  asker's name), set on the run and on every annotation; the stage line enters it without the
  options of :data:`workflows.triage.ask.UNDIGESTED_OPTIONS` (``workers``: how many questions are
  in flight at once, not what is asked), so changing them re-scopes nothing;
* substage ``ask`` (D32): when the gate has questions and an asker is plugged in
  (:func:`stack.gate.asker_for`), the asker gets the scoped rows lacking a final carried answer
  (D724: every scoped row, also outside the question scope; count ``signal_pairs`` = the requested
  pairs outside it); without an asker (M2-M3, D37) or under ``--only select`` nothing is asked;
* substage ``select`` (pure, always runs): per scoped row, the ``QUESTION`` list (carried, asked,
  or the fill: ``skipped[not_requested]`` when nothing was asked, ``skipped[not_needed]`` outside
  the question scope, ``not_attempted[limit]`` for a pair the asker left out; outside the question
  scope a carried answer only a later ask could resolve, ``not_attempted`` or ``failed_transient``,
  is not carried but replaced by the fill, D697, count ``settled_out_of_question_scope``: carried it
  kept the row due and the run incomplete for good; since D724 the ask substage asks it again), on a
  row outside the question scope every answer that was not carried as a stored answer (asked this
  run, or carried from a signal) stored as a **signal** (D724, :func:`stack.gate.signal_row`: the
  fill with the answer's verdict, confidence and evidence id; count ``signals`` = those with a
  verdict), so it decides nothing and the row decides as before; the decision of
  :func:`stack.gate.decide` (arm 7, the carried standing, for a row not ``fetch`` by ``sql:`` whose
  primary role is ``ok``: its current annotation when decided, else the chain walk's; on a row
  with ``former_doc_ids`` the strongest of that and the former ids' standings,
  :func:`stack.gate.merged_standing`, D263), the
  ``ANN<TRIAGE>`` (basis ``fields`` = ``metadata.fields_digest``,
  S9), state ``identified → triaged`` (``triaged``/``acquired`` keep their state: a re-decision
  is not a demotion, X19), the flat block restamped.

**Accepted configs** (D758, :mod:`stack.plan` "Accepted configs"; :mod:`stack.gate` "Accepted answers"): per
question, the line's ``accepted:`` entries name earlier question digests whose stored ``ok`` answers and signals
count. Before any call the line's ``target`` of each question must be its digest, and each accepted digest the
``config_digest`` of some prior ``triage_evidence`` row whose ``prompt_name`` is the entry's ``name`` (else the run
is refused). The run record's ``selection`` is the line's (target per question, accepted list, selection digest).
Accepted answers are carried and decide like current ones (count ``carried.accepted``: the pairs), keeping their
own digest.

**Evidence carry** (§3.1: triage reads the ``triage_evidence`` prior and writes
``triage_evidence``; D129: the evidence file is cumulative, a chain of one that keeps the history):
the asker returns the cumulative evidence; when no asker ran (none plugged in, ``--only select``,
nothing to ask), the prior rows are written unchanged, never an empty file that the next
run would take as its prior.

**Counts** (passes, ``stack build --passes``): ``carried`` = rows of the head the run asked no
question about (out of scope, or every answer carried); the asker's transport requests are the
run's ``llm_calls`` (the stage runner's).

**Memory** (D780): the ``documents`` head stays Arrow. Scope, question scope and carried answers are worked out
:data:`stack.gate.BATCH_ROWS` rows at a time (:func:`stack.gate.row_batches`); the gate view is the head itself when
every row is scoped (a gate or config change: 2026-10-31, when the whole head as Python rows, twice, plus the
evidence file twice, took the build past its container's memory); only the rows the asker is sent become Python rows
together; the part is the scoped rows' Arrow with ``annotations``, ``state``, ``run_id`` and ``updated_at`` replaced
(built a batch at a time); the prior evidence is carried, and returned by the LLM asker, as Arrow.

``scope_complete`` is false when any question of the part ended ``not_attempted`` (left out by the
asker, cut by the budget or a halt, or carried unasked, i.e. in the question scope while the ``ask``
substage did not run: count ``not_attempted.carried``, D697): §3.3's planned stop, the run ends
``ok`` with ``scope_complete = false`` and the next run asks those questions. A signal pair
(D724: a row outside the question scope) left ``not_attempted`` does not make the scope incomplete:
it decides nothing, so it holds no build back; it is counted ``signal_not_attempted`` and
:func:`stack.gate.answer_due` brings its row into the next run's scope. The asker gets the question
scope's rows first, so a budget stop reaches the signals before an answer that decides.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack import gate
from stack.plan import StageLine
from stack.schemas.families import family
from stack.stage_runner import StageContext, StageError, StageResult
from workflows.triage.ask import OPTIONS as ASK_OPTIONS
from workflows.triage.ask import UNDIGESTED_OPTIONS

SUBSTAGES = ("ask", "select")
#: The line's known ``options`` (D434), read by :func:`workflows.triage.ask.llm_asker`
OPTIONS: frozenset[str] = ASK_OPTIONS
PURE_SUBSTAGES = ("select",)
TRIAGE_BLOCKS = ["annotations"]
EVIDENCE = "triage_evidence"

Row = dict[str, Any]


def _input(ctx: StageContext) -> str:
    docs = [k for k, ch in ctx.inputs.items() if ch.family == "documents"]
    if len(docs) != 1:
        raise StageError(f"triage: needs exactly one documents input (--in data/documents/<series>/), got {docs}")
    if ctx.filter is not None:
        raise StageError(f"triage: --filter {ctx.filter!r} is not supported (the scope is §3.1's)")
    return docs[0]


def digest_line(line: StageLine) -> StageLine:
    """The stage line as it enters the triage ``config_digest``: without the options that only
    steer how the run asks (:data:`workflows.triage.ask.UNDIGESTED_OPTIONS`)."""
    if not UNDIGESTED_OPTIONS & set(line.options):
        return line
    return line.model_copy(update={"options": {k: v for k, v in line.options.items()
                                               if k not in UNDIGESTED_OPTIONS}})


def _former(row: Row) -> list[str]:
    """The ids ``row`` absorbed or split from (``identity.former_doc_ids``)."""
    return list(row["identity"]["former_doc_ids"] or [])


def _accepted(ctx: StageContext, qs: list[gate.Question], prior: pa.Table | None) -> dict[str, tuple[str, ...]]:
    """The run's accepted question digests per question (:func:`stack.gate.accepted_answers`), after the stage-start
    check (module docstring, Accepted configs); sets the run's ``selection``. ``prior``: the prior
    ``triage_evidence`` view (its ``config_digest`` and ``prompt_name`` are read)."""
    line = ctx.plan_stage
    ctx.selection = line.selection({q.name: q.config_digest for q in qs})
    for q in qs:
        want = line.target_of(q.name)
        if want is not None and want != q.config_digest:
            raise StageError(f"triage: the line's target of {q.name} {want[:12]}… is not the question's digest "
                             f"({q.config_digest[:12]}…): its pin changed since the accepted list was written")
    names: dict[str, set[str]] = {}
    if line.accepted and prior is not None:
        pairs = prior.select(["config_digest", "prompt_name"]).group_by(["config_digest", "prompt_name"]).aggregate([])
        for c, p in zip(pairs["config_digest"].to_pylist(), pairs["prompt_name"].to_pylist(), strict=True):
            names.setdefault(c, set()).add(p or "")
    for e in line.accepted:
        where = f"triage: accepted {e.config[:12]}… ({e.question}, {e.name})"
        if e.config not in names:
            raise StageError(f"{where} is no config of the prior triage_evidence (pass --in "
                             "data/triage_evidence/<series>/)")
        if e.name not in names[e.config]:
            raise StageError(f"{where}: name is not a prompt name of its evidence ({sorted(names[e.config])})")
    return gate.accepted_answers(line, qs)


def _retry_view(prior: pa.Table | None) -> dict[str, Row]:
    """``evidence_id`` → the fields of a prior evidence row the retry rule reads
    (:func:`stack.annotate.evidence.prior_decision`: ``status``, ``attempts``, ``retry_after``), for
    :func:`stack.gate.question_due` and :func:`stack.gate.answer_due`."""
    if prior is None or prior.num_rows == 0:
        return {}
    cols = ["evidence_id", "status", "attempts", "retry_after"]
    return {r["evidence_id"]: r for rows in gate.row_batches(prior.select(cols)) for r in rows}


def run(ctx: StageContext) -> StageResult:
    doc_key = _input(ctx)
    plan = ctx.plan
    now = dt.datetime.now(dt.UTC)
    asker = gate.asker_for(plan, ctx.plan_stage)
    cfg = gate.triage_config_digest(digest_line(ctx.plan_stage), plan.gate, asker)
    ctx.config_digest = cfg
    gate.store_gate(ctx.blobs, plan.gate)
    cls_ = gate.clauses(plan.gate)
    qs = gate.questions(plan)
    head = ctx.view(doc_key)
    prior_tbl: pa.Table | None = ctx.view(EVIDENCE) if EVIDENCE in ctx.prior else None
    will_ask = bool(qs) and asker is not None and (ctx.only is None or "ask" in ctx.only)
    accepted = _accepted(ctx, qs, prior_tbl)
    due_now = gate.clock()
    ev_by_id = _retry_view(prior_tbl) if will_ask else {}
    groups = gate.channel_groups()
    wants = plan.acquire.wants
    hp = gate.html_is_primary(plan)  # D485: an ok arXiv html role counts as the primary full_text

    def askable(r: Row) -> bool:  # D724: whether the asker would send a question for the row
        return gate.can_ask(asker, {**r, **gate.gate_columns(r, groups)})

    # pass 1 over the head, in batches (D780): the scope, and arm 7's standing candidates and chain walk
    scoped_ix: list[int] = []
    cands: list[tuple[int, str, list[str]]] = []  # standing candidates: (head index, doc_id, former ids)
    walk: set[str] = set()
    due = answers_due = 0
    i = -1
    for rows in gate.row_batches(head):
        for r in rows:
            i += 1
            if gate.in_scope(r, cfg):
                pass
            elif will_ask and gate.question_due(r, qs, ev_by_id, due_now):
                due += 1
            elif will_ask and gate.answer_due(r, qs, ev_by_id, due_now, askable=askable,
                                              have_prior=EVIDENCE in ctx.prior, accepted=accepted):
                answers_due += 1
            else:
                # arm 7: the standing of rows outside the question scope because their primary role is ok
                if gate.standing_due(r, wants, html_primary=hp):
                    cands.append((i, r["doc_id"], _former(r)))
                    walk.add(r["doc_id"])
                    walk.update(_former(r))  # D263
                continue
            scoped_ix.append(i)
            if gate.primary_role_ok(r, wants, html_primary=hp):
                if not gate.is_decided(gate.triage_of(r)):
                    walk.add(r["doc_id"])
                walk.update(_former(r))  # D263
    del ev_by_id
    stands = gate.standings(ctx.store_root, ctx.inputs[doc_key], walk) if walk else {}
    added = [i for i, d, former in cands if d in stands or any(f in stands for f in former)]
    standing_added = len(added)
    scoped_ix += added
    del cands, walk, added
    # the scoped rows as Arrow, the head itself when every row is scoped (no copy)
    whole = len(scoped_ix) == head.num_rows and scoped_ix == list(range(head.num_rows))
    scoped_tbl = head if whole else head.take(pa.array(scoped_ix, pa.int64()))
    del scoped_ix
    view = gate.gate_view(scoped_tbl, groups)
    hits = gate.sql_hits(view, cls_)
    sql_fetch = {d for d, ix in hits.items() if ix}
    # pass 2 over the scoped rows: the question scope and the carried answers.
    # D697, D724: outside the question scope a carried answer that only a later ask could resolve
    # (not_attempted, failed_transient) is not carried: the ask substage asks it again (as a signal),
    # and without an ask the row is settled with the fill
    qscope: set[str] = set()
    scoped_ids: list[str] = []
    answers: dict[tuple[str, str], Row] = {}
    real: set[tuple[str, str]] = set()  # carried answers that were stored as answers, not as signals (D724)
    settled = n_accepted = 0
    by_name = {q.name: q for q in qs}
    for rows in gate.row_batches(scoped_tbl):
        for r in rows:
            d = r["doc_id"]
            scoped_ids.append(d)
            out_of_qscope = not gate.in_question_scope(r, d in sql_fetch, wants, html_primary=hp)
            if not out_of_qscope:
                qscope.add(d)
            t = gate.triage_of(r)
            stored = {a["question"]: a for a in ((t or {}).get("payload") or {}).get("questions") or []}
            for n, a in gate.carried_answers(r, qs, accepted).items():
                n_accepted += a["config_digest"] != by_name[n].config_digest
                if out_of_qscope and a["status"] in gate.RESOLVABLE:
                    settled += 1
                else:
                    answers[(d, n)] = a
                    if not gate.is_signal(stored.get(n)):
                        real.add((d, n))
    carried = len(answers)
    evidence: pa.Table | list[Row] | None = prior_tbl  # carried unchanged unless the asker returns the new set
    asked = False
    requested: set[tuple[str, str]] = set()
    ask_ids: set[str] = set()

    with ctx.substage("ask") as sub:
        if sub.active and qs and asker is not None:
            asked = True
            no_basis_final = not gate.asks_without_basis(asker)  # D527: title_basis asks them
            # D724: every scoped row is asked, not only the question scope (its answers decide)
            requested = {(d, q.name) for d in scoped_ids for q in qs
                         if not gate.is_final(answers.get((d, q.name)), no_basis_final=no_basis_final)}
            ask_ids = {d for d, _ in requested}
            # D724: the question scope's rows first, so a budget or max_rows stop cuts signals first.
            # Only the asked rows leave Arrow (D780).
            ask_view = view.filter(pc.is_in(view["doc_id"], value_set=pa.array(sorted(ask_ids), pa.string())))
            ask_rows = sorted(ask_view.to_pylist(), key=lambda r: (r["doc_id"] not in qscope, r["doc_id"]))
            del ask_view
            if ask_rows:
                res = asker.ask(ask_rows, qs, ctx)
                del ask_rows
                extra = sorted(set(res.answers) - requested)
                if extra:
                    raise gate.GateError(f"triage: the asker answered pairs it was not asked: {extra[:3]}")
                digests = {q.name: q.config_digest for q in qs}
                for (d, n), a in res.answers.items():
                    if a["question"] != n or a["config_digest"] != digests[n]:
                        raise gate.GateError(f"triage: answer for {d}/{n} names {a['question']!r} "
                                             f"with config_digest {a['config_digest'][:12]}…")
                    answers[(d, n)] = dict(a)
                evidence = res.evidence
                del res
            ctx.heartbeat()

    with ctx.substage("select"):
        n_dec = {"fetch": 0, "skip": 0, "pending": 0}
        arms = [0] * 8
        unreached = 0
        unasked = 0  # carried not_attempted pairs this run did not request (the ask substage did not run)
        signals = 0  # answers stored as signals on rows outside the question scope (D724)
        signal_unreached = 0  # signal pairs left not_attempted: the next run asks them; not an incomplete scope
        fill = "not_needed" if asked else "not_requested"
        origin = asker.origin if asker is not None else gate.ASKED_ORIGIN
        schema = family("documents").schema
        ann_type = schema.field("annotations").type
        new_ann: list[pa.Array] = []
        new_state: list[pa.Array] = []
        for rows in gate.row_batches(scoped_tbl):  # D780: the new columns per batch, the others stay the head's
            ann_b: list[Row] = []
            state_b: list[str] = []
            for r in rows:
                d = r["doc_id"]
                in_q = d in qscope
                qrows: list[Row] = []
                for q in qs:
                    found = answers.get((d, q.name))
                    if found is not None:
                        a = found
                    else:
                        if not asked:
                            a = gate.question_row(q, "skipped", "not_requested")
                        elif (d, q.name) in requested:
                            a = gate.question_row(q, "not_attempted", "limit")
                        else:
                            a = gate.question_row(q, "skipped", "not_needed")
                    signal = not in_q and (d, q.name) not in real
                    if a["status"] == "not_attempted" and signal:
                        signal_unreached += 1
                    elif a["status"] == "not_attempted":
                        unreached += 1
                        if found is not None and (d, q.name) not in requested:
                            unasked += 1
                    if signal:
                        # D724: outside the question scope an answer does not decide; it is kept as a signal
                        a = gate.signal_row(a, fill)
                        signals += a["verdict"] is not None
                    qrows.append(a)
                standing = None
                if d not in sql_fetch and gate.primary_role_ok(r, wants, html_primary=hp):
                    cur = gate.triage_of(r)
                    standing = cur if gate.is_decided(cur) else stands.get(d)
                    if _former(r):  # a merge never loses a member's fetch (D263)
                        standing = gate.merged_standing(standing, [stands.get(f) for f in _former(r)])
                dec = gate.decide(cls_, hits.get(d, set()), {a["question"]: a for a in qrows},
                                  asked_origin=origin, standing=standing)
                n_dec[dec.decision] += 1
                arms[dec.arm] += 1
                ann_b.append({**r["annotations"], gate.WORKFLOW: gate.annotation(
                    dec, qrows, basis_digest=r["metadata"]["fields_digest"], config_digest=cfg,
                    decided_at=now)})
                state_b.append("triaged" if r["state"] == "identified" else r["state"])
            new_ann.append(pa.array(ann_b, ann_type))
            new_state.append(pa.array(state_b, schema.field("state").type))
        n_out = scoped_tbl.num_rows
        out = scoped_tbl.select(schema.names)
        for name, arr in (("annotations", pa.chunked_array(new_ann, ann_type)),
                          ("state", pa.chunked_array(new_state, schema.field("state").type)),
                          ("run_id", pa.array([ctx.run_id] * n_out, schema.field("run_id").type)),
                          ("updated_at", pa.array([now] * n_out, schema.field("updated_at").type))):
            out = out.set_column(schema.get_field_index(name), schema.field(name), arr)
        del new_ann, new_state
        if not out.schema.equals(schema):
            out = out.cast(schema)
        if n_out > 1 and not pc.all(pc.less(out["doc_id"].slice(0, n_out - 1), out["doc_id"].slice(1))).as_py():
            out = out.sort_by("doc_id")  # standing rows come after the head's order
        cid = ctx.inputs[doc_key].chain_id
        if n_out or not ctx.writes_part("documents", chain_id=cid):  # S25-52: no empty part
            ctx.emit_part("documents", out, TRIAGE_BLOCKS, chain_id=cid)
        ev_schema = family(EVIDENCE).schema
        if evidence is None:
            ev_tbl = ev_schema.empty_table()
        elif isinstance(evidence, pa.Table):
            ev_tbl = evidence if evidence.schema.equals(ev_schema) else evidence.cast(ev_schema)
        else:
            ev_tbl = pa.Table.from_pylist(evidence, schema=ev_schema)
        n_evidence = ev_tbl.num_rows
        ctx.emit_full(EVIDENCE, ev_tbl, TRIAGE_BLOCKS)

    ctx.counts.update(
        head=head.num_rows, scoped=n_out, out_of_scope=head.num_rows - n_out,
        fetch=n_dec["fetch"], skip=n_dec["skip"], pending=n_dec["pending"],
        **{f"arm{i}": arms[i] for i in range(1, 8)},
        questions_due=due, answers_due=answers_due, standing_due=standing_added, question_scope=len(qscope),
        carried_answers=carried, asked_pairs=len(requested),
        signal_pairs=sum(1 for d, _ in requested if d not in qscope), signals=signals,
        signal_not_attempted=signal_unreached,
        carried=head.num_rows - len(ask_ids),
        evidence=n_evidence, not_attempted=unreached)
    if settled:
        ctx.counts["settled_out_of_question_scope"] = settled
    if n_accepted:
        ctx.counts["carried.accepted"] = n_accepted
    if unasked:
        ctx.counts["not_attempted.carried"] = unasked
    return StageResult(scope_complete=unreached == 0, rows_touched=n_out)
