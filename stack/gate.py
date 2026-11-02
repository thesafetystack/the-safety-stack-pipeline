"""The triage gate (§3.1 "The triage gate", §3.5 ``gate:``; D19, D37): the gate view, ``sql:``
clause evaluation (duckdb), the seven-arm decision table, ``wanted_by``, ``gate_digest``, the
triage scope and the **asker protocol** (the seam T4.2 plugs the LLM asker into).

**Gate view** (D19): the ``documents`` row (every column, so a clause reads ``metadata.abstract``,
``identity.channels``, ``content.current_text_status`` …) plus two derived columns:

* ``inband_text_sha256`` = :func:`stack.checks.digests.inband_excerpt_sha256` of the row (D75: the
  anchor's in-band entry of best pointer rank, then newest, then smallest digest; NULL if none);
* ``channel_group`` = ``channels.yaml``'s group of the anchor member's channel.

A ``sql:`` clause is a duckdb predicate over that view (``SELECT doc_id FROM gate_view WHERE
(<sql>)``, ``TimeZone = 'UTC'``, the :mod:`stack.query` macros registered); a clause that does not
evaluate raises :class:`GateError` naming it. A ``rule:`` clause (track S, S5) names a triage
source rule of :mod:`stack.rules`; it is a ``sql:`` clause in every respect below (positive when
it matches, conclusively negative otherwise, ``origin = 'rule'``, its fetches outside the question
scope), its SQL the rule's compiled predicate as the plan carries it (``GateClause.rule_sql``,
which every gate digest covers).

**Clauses and questions.** ``plan.gate.consumers`` maps a consumer to its clause list, in plan
order (:func:`clauses`). A ``question:`` clause names a question; one question name is asked once
per row however many consumers list it (same ``prompt`` required, else :class:`GateError`), and
each clause applies its own ``fetch_on``. :func:`questions` gives the distinct questions in plan
order with their ``config_digest`` = ``H({question, prompt, pin})`` where ``pin`` is
``plan.prompts[prompt]`` (``fetch_on`` is not in it: a ``fetch_on`` edit re-selects, it never
re-asks).

**Decision table** (:func:`decide`; §3.1, first match wins). A clause is *positive* when it is a
matching ``sql:`` clause or a question clause whose question is ``ok`` with a verdict in the
clause's ``fetch_on``; *conclusively negative* when it is a non-matching ``sql:`` clause or a
question clause whose question is ``ok`` with any other verdict.

1. some clause positive → ``fetch``, ``ok``;
2. every clause conclusively negative → ``skip``, ``ok`` (an all-``sql:`` gate always lands here
   when nothing matched);
3. no clause positive and every unresolved question is ``skipped[no_basis]`` → ``pending``,
   ``skipped[no_basis]``;
4. no clause positive, no unresolved question can still resolve (``not_attempted``,
   ``failed_transient``) and at least one is terminal (``failed_permanent``, ``quarantined``) →
   ``pending``, ``skipped[gate_failed]``;
5. no clause positive and some question is ``not_attempted``/``failed_transient`` → ``pending``
   with the first such question's status and ``error_class`` (plan order);
7. **carried standing** (T5.5; checked before 6): arm 6's conditions, every question row a
   re-derived fill (``skipped[not_needed|not_requested]``), and the caller passes a *standing*,
   only for a row outside the question scope because its primary role is already ``ok``
   (:func:`primary_role_ok`) → the standing's ``decision`` (``fetch``/``skip``), ``ok``,
   ``wanted_by`` = the standing's filtered to the gate's consumers (a ``fetch`` left with none is
   not carried: arm 6), ``origin`` = the standing's;
6. otherwise → ``pending``, ``skipped[not_needed]``.

**Standing** (:func:`standings`): the row's newest *decided* triage annotation (``ok``, ``fetch``
or ``skip``): its current one when decided, else the newest decided one for its ``doc_id`` in the
pinned ``documents`` chain's earlier files, newest to oldest, past the newest full file along
``base`` while the files are in ``data/``. Already-acquired documents so keep their triage
standing when a later gate does not ask about them (the 294 rows of RUNLOG (C) 15:36-16:01 that a
``gate:`` edit re-decided ``skipped[not_needed]``). A document that absorbed or split from others
(``identity.former_doc_ids``) stands on the strongest of its own standing and theirs
(:func:`merged_standing`, D263; R3-4): ``fetch`` over ``skip``, so a merge never loses a
member's ``fetch``.

``wanted_by`` = the consumers with a positive clause, sorted. ``origin`` = ``'rule'`` unless an
``ok`` question answer took part in the decision (arm 1 through a question clause, or arm 2 with
questions), then the asker's ``origin`` (:data:`ASKED_ORIGIN` by default).

**No asker (M2-M3, D37).** Every question without a carried answer is ``skipped[not_requested]``.
Such a row with no positive clause lands in **arm 6** (``pending``, ``skipped[not_needed]``),
not arm 3: (a) arm 3's condition is "every question lacks a basis", and a ``fields`` basis exists
— the question was not asked, which is not the same fact; (b) acquire fetches ``no_basis`` rows
once (§3.1, D22 ``no_basis_fetches``), so arm 3 would turn "no asker" into "fetch every row the
``sql:`` clauses did not select", the opposite of D37's sql-only scope; ``pending`` under arm 6
is no permission to fetch; (c) arm 6 is the table's fall-through, and the row is re-triaged when
an asker arrives because the asker's ``name`` is part of the triage ``config_digest``
(:func:`triage_config_digest`), so every such row is config-stale and back in scope.

**Scope** (:func:`in_scope`; §3.1): unmasked, not ``retired``, and ``identified``, or no triage
annotation, or ``annotations.triage.basis_digest <> metadata.fields_digest``, or its
``config_digest`` ≠ the current :func:`triage_config_digest`; the stage adds the rows with a
question due (:func:`question_due`) or an answer due (:func:`answer_due`, D724) when an asker will
run, and the rows whose standing is due (:func:`standing_due`: ``pending``, ``skipped[not_needed]``,
primary role ``ok``, a standing in the chain), which repairs rows an earlier ``gate:`` edit already
set to ``not_needed``. **Question scope** (:func:`in_question_scope`; the rows whose answers decide;
since D724 the asker is asked about every scoped row, and an answer outside the question scope is a
signal, below): rows in scope, not ``fetch`` by a ``sql:`` clause, whose primary role
is not ``ok`` (primary role = the first role of ``plan.acquire.wants[<anchor channel>]``; ``ok`` =
a ``content.roles[]`` entry of that role with ``status = 'ok'`` whose ``source_id`` is NULL or one of the
row's current ``identity.member_ids``; no wants entry → not ``ok``; D785: a gather that moves a member out
leaves its roles on the row until acquire regroups, and triage runs in between). When
an acquire line of the plan sets ``options.arxiv_pdf: on_miss | never`` (:func:`html_is_primary`,
D485), an ``ok`` arXiv ``html`` role also counts for a primary role ``full_text``: under those
settings most arXiv papers never get a PDF, and their HTML is what was fetched. A primary role
of any status also counts as ``ok`` while it still points at a held raw snapshot (D704, supersedes
D621's ``full_text``-only rule): the gate stands on the content held, and a role's latest fetch or
recheck outcome (set aside, ``withdrawn``, ``empty``, failed) drives only acquire, which can change
it after triage in the same build. A row is re-triaged when its triage basis or config changes
(:func:`in_scope`), never because a role's status did.

**Carried answers** (:func:`carried_answers`): a ``QUESTION`` of the row's current triage
annotation is reused when the annotation's ``basis_digest`` equals the row's ``fields_digest``,
the ``QUESTION.config_digest`` equals the question's current digest, and it is not one of the
re-derived fills (``skipped[not_needed]``, ``skipped[not_requested]``), or it is a fill that
carries a verdict (a *signal*, below), which is carried as the ``ok`` answer it records; the
triage stage drops a carried :data:`RESOLVABLE` answer of a row outside the question scope (D697,
D724: it is asked again there when an asker runs, and settled with the fill when none does). This
is what makes ``triage --only select`` after a ``gate:`` edit (new ``sql:`` clauses, consumers,
``fetch_on``) re-decide every row with zero asker calls.

**Accepted answers** (D758, :mod:`stack.plan` "Accepted configs"; :func:`accepted_answers`): the triage line's
``accepted:`` entries of a question name earlier question digests. A stored answer under one of them that is
``ok`` or a signal (never a failure) is carried as if current (:func:`carried_answers`), keeping its own
``config_digest`` (the label of the prompt that judged it): it is final (the asker is not asked) and settled for
:func:`answer_due`. A triage config move re-decides rows from the carried answers, accepted ones included,
without calls.

**Signals** (D724): every scoped row is asked, also outside the question scope (a ``sql:`` fetch,
or a primary role that is ``ok``), so every document gets a verdict under the current question
digest; but outside the question scope an answer obtained there must not decide (the row decides
as it always did: arm 1 on its ``sql:`` clause, arm 7 on its standing, else arm 6). Such an answer
is stored as a *signal* (:func:`signal_row`): the fill ``select`` gives the question there
(``skipped[not_needed]``, ``skipped[not_requested]`` when the ask substage did not run) with the
answer's ``verdict``, ``confidence`` and ``evidence_id``; an answer that is not ``ok`` keeps only
its ``evidence_id`` (none for a ``not_attempted`` pair or ``no_basis``). :func:`decide` reads a
question row's ``status`` and ``error_class`` only, so a signal decides exactly as the fill did
before, by construction (X14's :func:`recompute` too, except on a row now in the question scope,
where triage would carry the signal as an answer, D785); a reader that wants the verdict of every document reads
``verdict`` whatever the status (:func:`verdict_of`). A row lacking a settled answer is due
(:func:`answer_due`): no current-digest row for a question, or a fill that is not a settled
signal (a verdict; or evidence the retry rule carries; or, with neither, a row the asker cannot
ask, :func:`can_ask`). So the first run on this code asks every document that was never asked
under the current question digest, and a question digest change (a new prompt name, model or
template) makes every document due again.

**Asker protocol — the seam for T4.2.** An asker is any object with::

    name: str        # stable id (e.g. "llm:<model>"), part of the triage config_digest
    origin: str      # the ANN origin of rows an answer decided ('ai_check')
    def ask(self, rows: list[dict], questions: list[Question], ctx: StageContext) -> AskResult: ...
    def can_ask(self, row: dict) -> bool: ...  # optional (D724, :func:`can_ask`); absent: True

``rows`` are gate-view rows (the documents row plus ``inband_text_sha256``, ``channel_group``) of
the scope (D724; before, of the question scope only) that lack a *final* carried answer (final = ``ok``, ``failed_permanent``,
``quarantined``, ``skipped[no_basis]``) for at least one question; ``questions`` are the
:class:`Question` entries (name, prompt, pin, config_digest). The asker returns an
:class:`AskResult`: ``answers`` maps ``(doc_id, question name)`` to a ``QUESTION`` dict (A.2:
``question, status, error_class, verdict, confidence, evidence_id, config_digest``; the
``config_digest`` must be the question's), and ``evidence`` holds the ``triage_evidence`` rows
(A.5: EVI + ``question, verdict, confidence, reasoning`` + T2) the stage writes. It may answer a
subset: a requested pair left out is ``not_attempted[limit]`` (and the run ends
``scope_complete = false``); an answer for a pair not requested is a :class:`GateError`. The prior
evidence (``--in data/triage_evidence/<series>/``, the stage line's ``prior: [triage_evidence]``) is
``ctx.prior`` / ``ctx.view(...)``; reuse and retry policy (``retry_after``, caps, the response
cache) is the asker's. ``select`` never calls the asker.

**The gate in the store.** Every triage run puts the gate's canonical JSON into the store's
blobs (:func:`store_gate`; blob digest = ``gate_digest``; flat blobs, D360), so X14 recomputes against the
gate a build records by ``gate_digest`` even after the plan file changed (:func:`load_gate`).

**Plugging it in.** ``workflows/triage/main.py`` calls :func:`asker_for` once per run; it returns
``ASKER_HOOK(plan, stage_line)`` when the hook is set (tests: a fake, ``tests/test_triage_rules.py``,
or ``None`` for the no-asker case), else the LLM asker of ``workflows/triage/ask.py`` (T4.2) when
the gate has question clauses, else ``None``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

import duckdb
import pyarrow as pa
import pyarrow.compute as pc

from stack import config as stack_config
from stack.checks.digests import H, inband_excerpt_sha256
from stack.core.hashes import jcs
from stack.plan import STORED_GATE, Gate, Plan, StageLine, load_plan
from stack.query import one_statement, sql_connection
from stack.schemas.families import family
from stack.schemas.payloads.registry import payload
from stack.store.blobs import BlobStore
from stack.store.parquet import read_family

if TYPE_CHECKING:
    from stack.stage_runner import StageContext
    from stack.store.chain import Chain

Row = dict[str, Any]

WORKFLOW = "triage"
BASIS_KIND = "fields"
RULE_ORIGIN = "rule"
#: The ANN origin of a row decided by a question answer, unless the asker says otherwise.
ASKED_ORIGIN = "ai_check"
VIEW_TABLE = "gate_view"
#: The two derived columns of the gate view (D19), appended after the ``documents`` columns.
VIEW_EXTRA = pa.schema([pa.field("inband_text_sha256", pa.string()),
                        pa.field("channel_group", pa.string())])

#: Question statuses a later ask may still turn into an answer (arm 5).
RESOLVABLE = ("not_attempted", "failed_transient")
#: Terminal question statuses (arm 4).
TERMINAL = ("failed_permanent", "quarantined")
#: Fills ``select`` derives itself; never carried.
REDERIVED = (("skipped", "not_needed"), ("skipped", "not_requested"))
#: Triage decisions a row can stand on (arm 7's standing).
DECIDED = ("fetch", "skip")
#: Carried answers the asker is not asked again about.
FINAL = (("ok", None), ("failed_permanent", None), ("quarantined", None), ("skipped", "no_basis"))


class GateError(Exception):
    """A gate that cannot be evaluated: a ``sql:`` clause that does not run, a question listed
    with two prompts, an asker answering what it was not asked, an unknown anchor channel."""


# ---------------------------------------------------------------- clauses and questions
@dataclass(frozen=True)
class Clause:
    """One clause of ``plan.gate``: ``consumer``, its position in the plan's clause list
    (``index``, global, plan order) and the clause itself."""

    index: int
    consumer: str
    sql: str | None = None
    question: str | None = None
    prompt: str | None = None
    fetch_on: tuple[str, ...] = ()
    rule: str | None = None  # a ``rule:`` clause's name; ``sql`` is then its compiled predicate

    @property
    def ref(self) -> str:
        return f"{self.consumer}[{self.index}]" + (f" (rule {self.rule})" if self.rule else "")


@dataclass(frozen=True)
class Question:
    """A distinct question of the gate: ``name``, ``prompt`` (the plan's prompt key), ``pin``
    (``plan.prompts[prompt]``, may be empty) and ``config_digest`` (module docstring)."""

    name: str
    prompt: str
    pin: Mapping[str, Any] = field(default_factory=dict)
    config_digest: str = ""


def clauses(gate: Gate) -> list[Clause]:
    """Every clause of ``gate`` in plan order (consumers in file order, clauses in list order)."""
    out: list[Clause] = []
    for consumer, cls_ in gate.consumers.items():
        for cl in cls_:
            out.append(Clause(len(out), consumer, cl.predicate, cl.question, cl.prompt,
                              tuple(cl.fetch_on or ()), cl.rule))
    return out


def questions(plan: Plan) -> list[Question]:
    """The distinct questions of ``plan.gate`` in plan order, with their config digests."""
    seen: dict[str, Question] = {}
    for c in clauses(plan.gate):
        if c.question is None:
            continue
        assert c.prompt is not None  # GateClause validates it
        prev = seen.get(c.question)
        if prev is not None:
            if prev.prompt != c.prompt:
                raise GateError(f"gate: question {c.question!r} listed with prompts {prev.prompt!r} "
                                f"and {c.prompt!r}; one question has one prompt")
            continue
        pin = dict(plan.prompts.get(c.prompt) or {})
        seen[c.question] = Question(c.question, c.prompt, pin,
                                    H({"question": c.question, "prompt": c.prompt, "pin": pin}))
    return list(seen.values())


def gate_digest(gate: Gate) -> str:
    """D19: sha256 of the JCS of ``plan.gate`` in its file shape (= ``Plan.gate_digest``; a
    ``rule:`` clause's compiled ``rule_sql`` included)."""
    return H(gate.file_shape())


def store_gate(blobs: BlobStore, gate: Gate) -> str:
    """Put the gate's canonical JSON in the store's blobs (D360); its blob digest is
    :func:`gate_digest` (both are sha256 of the same JCS bytes), so X14 finds the gate a build
    records by ``gate_digest`` without the plan file."""
    digest = blobs.put(jcs(gate.file_shape()).encode("utf-8"))
    assert digest == gate_digest(gate)
    return digest


#: D690: a gate stored before the gate view's column ``inband_text_digest`` was renamed names it in its SQL;
#: :func:`load_gate` reads it under the new name (the stored blob and its digest stay as they are). Kept by D693
#: when the old-format readers went: gates are content-addressed blobs (their sha256 is the builds' and the triage
#: annotations' ``gate_digest``), so the one-off upcast did not rewrite them, and X14 reads the gate of every build
#: made before the rename (every build until the first one on renamed code) for as long as those builds are kept.
_RENAMED_VIEW_COLUMN = re.compile(r"\binband_text_digest\b")


def load_gate(blobs: BlobStore, digest: str) -> Gate | None:
    """The gate :func:`store_gate` stored under ``digest``, or ``None`` when it is not there. A
    ``rule:`` clause keeps its stored ``rule_sql`` (what that run evaluated), not today's compile; a gate
    stored before D690 has its SQL's ``inband_text_digest`` read as ``inband_text_sha256``."""
    if not blobs.exists(digest):
        return None
    text = _RENAMED_VIEW_COLUMN.sub("inband_text_sha256", blobs.get(digest).decode("utf-8"))
    return Gate.model_validate(json.loads(text), context={STORED_GATE: True})


def triage_config_digest(stage_line: StageLine, gate: Gate, asker: Asker | None) -> str:
    """The triage run's ``config_digest`` and every triage annotation's: ``H`` of the stage
    line's digest, :func:`gate_digest` and the asker's ``name`` (``None`` without one)."""
    return H({"stage": stage_line.digest(), "gate": gate_digest(gate),
              "asker": asker.name if asker is not None else None})


# ---------------------------------------------------------------- the gate view
def channel_groups(config_dir: Path | None = None) -> dict[str, str]:
    """``channels.yaml``: channel id → ``channel_group`` (dir, else ``STACK_CONFIG_DIR``, else
    the repo's ``configs/``)."""
    d = config_dir if config_dir is not None else (
        Path(os.environ["STACK_CONFIG_DIR"]) if os.environ.get("STACK_CONFIG_DIR") else None)
    return {c.id: c.channel_group for c in stack_config.load(d).channels.channels}


def anchor_channel(row: Mapping[str, Any]) -> str:
    """The channel of the row's anchor member."""
    ident = row["identity"]
    anchor = ident["anchor_source_id"]
    for m in ident["members"] or []:
        if m["source_id"] == anchor:
            return str(m["channel"])
    raise GateError(f"{row['doc_id']}: anchor {anchor} is not among its members")


def gate_columns(row: Mapping[str, Any], groups: Mapping[str, str]) -> Row:
    """The two derived gate-view columns of one ``documents`` row."""
    ch = anchor_channel(row)
    if ch not in groups:
        raise GateError(f"{row['doc_id']}: anchor channel {ch!r} not in channels.yaml")
    return {"inband_text_sha256": inband_excerpt_sha256(row["identity"]["anchor_source_id"],
                                                         (row["inband"] or {}).get("entries")),
            "channel_group": groups[ch]}


#: Rows turned into Python rows at a time by :func:`row_batches` (D780): triage and :func:`gate_view` hold the
#: ``documents`` head as Arrow, never the whole head as Python rows (about 24 KB a row; a gate or config change
#: scopes every row, as on 2026-10-31).
BATCH_ROWS = 8192


def row_batches(table: pa.Table) -> Iterator[list[Row]]:
    """``table``'s rows as Python rows, :data:`BATCH_ROWS` at a time, in order."""
    for off in range(0, table.num_rows, BATCH_ROWS):
        yield table.slice(off, BATCH_ROWS).to_pylist()


def gate_view(table: pa.Table, groups: Mapping[str, str]) -> pa.Table:
    """``table`` (``documents`` rows) with ``inband_text_sha256`` and ``channel_group`` appended; the derived
    columns are computed a batch at a time (:func:`row_batches`, D780)."""
    extra = [gate_columns(r, groups) for rows in row_batches(table.select(["doc_id", "identity", "inband"]))
             for r in rows]
    for f in VIEW_EXTRA:
        table = table.append_column(f, pa.array([e[f.name] for e in extra], f.type))
    return table


def sql_hits(view: pa.Table, cls_: Sequence[Clause]) -> dict[str, set[int]]:
    """``doc_id`` → indices of the ``sql:`` clauses its gate-view row matches."""
    hits: dict[str, set[int]] = {}
    sqls = [c for c in cls_ if c.sql is not None]
    if not sqls or view.num_rows == 0:
        return hits
    con = sql_connection()  # plan-supplied SQL: no file access, one statement
    try:
        con.register(VIEW_TABLE, view)
        for c in sqls:
            try:
                ids_ = con.execute(one_statement(f"SELECT doc_id FROM {VIEW_TABLE} WHERE ({c.sql})")).fetchall()
            except duckdb.Error as exc:
                raise GateError(f"gate clause {c.ref} sql {c.sql!r} does not evaluate: {exc}") from exc
            for (d,) in ids_:
                hits.setdefault(d, set()).add(c.index)
    finally:
        con.close()
    return hits


# ---------------------------------------------------------------- scope
def triage_of(row: Mapping[str, Any]) -> Row | None:
    return (row.get("annotations") or {}).get(WORKFLOW)


def in_scope(row: Mapping[str, Any], config_digest: str) -> bool:
    """§3.1's triage scope (module docstring)."""
    if row["state"] == "retired" or row["identity"]["is_masked"]:
        return False
    t = triage_of(row)
    return (row["state"] == "identified" or t is None
            or t["basis_digest"] != row["metadata"]["fields_digest"]
            or t["config_digest"] != config_digest)


#: ``options.arxiv_pdf`` values of an acquire line under which the arXiv HTML stands for the PDF
#: (``workflows/acquire/carry.py`` ``ARXIV_PDF_MODES``; D480, D485).
HTML_PRIMARY_MODES: frozenset[str] = frozenset({"on_miss", "never"})


def html_is_primary(plan: Plan | None) -> bool:
    """Whether an ``ok`` arXiv ``html`` role counts as a primary ``full_text`` (module docstring,
    Scope; D485): some ``acquire`` line of ``plan`` sets ``options.arxiv_pdf`` to ``on_miss`` or
    ``never``. ``None`` (no plan) → ``False``."""
    return plan is not None and any(
        line.stage == "acquire" and (line.options or {}).get("arxiv_pdf") in HTML_PRIMARY_MODES
        for line in plan.stages)


def _held_snapshot(content: Mapping[str, Any], r: Mapping[str, Any]) -> bool:
    """A role that still points at a held raw snapshot, whatever its status (D704, supersedes D621's
    ``full_text``-only rule): text fetched earlier that a later rule or recheck left behind without
    dropping it. Acquire's ``_apply`` moves ``snapshot_id`` only when it mints a snapshot, so a
    recheck of an ``ok`` role that answers ``skipped[withdrawn]``, ``empty`` (an arXiv HTML's
    ``no_html:http_404``) or a failure keeps it; the arXiv PDF rule's ``skipped[not_requested]`` and
    the one-fetch rule's ``skipped[not_needed]`` set a fetched PDF aside with its snapshot kept."""
    sid = r.get("snapshot_id")
    return sid is not None and any(s["snapshot_id"] == sid and s["kind"] == "raw"
                                   for s in content.get("snapshots") or [])


def primary_role_ok(row: Mapping[str, Any], wants: Mapping[str, Sequence[str]], *,
                    html_primary: bool = False) -> bool:
    """Whether the row's primary role (first of ``wants[<anchor channel>]``) is ``ok`` or still points
    at a held raw snapshot (:func:`_held_snapshot`, D704); with ``html_primary``
    (:func:`html_is_primary`) an arXiv ``html`` role counts for ``full_text`` the same way. Only the
    roles of the row's current members count (``source_id`` NULL or in ``identity.member_ids``, the
    roles acquire's ``carry`` keeps, D785): after a gather that moved a member out, the row keeps that
    member's roles until acquire regroups (``workflows/gather/carry.py``), and triage runs in between."""
    roles = wants.get(anchor_channel(row)) or ()
    content = row.get("content")
    if not roles or content is None:
        return False
    members = set(row["identity"]["member_ids"])
    primary = ("full_text", "html") if html_primary and roles[0] == "full_text" else (roles[0],)
    return any(r["content_role"] in primary and (r["source_id"] is None or r["source_id"] in members)
               and (r["status"] == "ok" or _held_snapshot(content, r))
               for r in content["roles"] or [])


def in_question_scope(row: Mapping[str, Any], sql_fetch: bool,
                      wants: Mapping[str, Sequence[str]], *, html_primary: bool = False) -> bool:
    """The question scope: a row not ``fetch`` by ``sql:`` whose primary role is not ``ok`` (``row``
    already in :func:`in_scope`), the rows whose answers decide; every scoped row is asked, and an
    answer outside this scope is stored as a signal (D724, :func:`signal_row`)."""
    return not sql_fetch and not primary_role_ok(row, wants, html_primary=html_primary)


# ---------------------------------------------------------------- standing (arm 7)
def is_decided(t: Mapping[str, Any] | None) -> bool:
    """A triage annotation a row can stand on: ``ok`` with decision ``fetch`` or ``skip``."""
    return (t is not None and t["status"] == "ok" and t["payload"] is not None
            and t["payload"]["decision"] in DECIDED)


def standing_due(row: Mapping[str, Any], wants: Mapping[str, Sequence[str]], *,
                 html_primary: bool = False) -> bool:
    """Whether ``row`` may be added to the scope for its standing (module docstring "Scope"):
    unmasked, not ``retired``, current triage ``pending``, ``skipped[not_needed]``, primary role
    ``ok``. The caller adds it only when :func:`standings` finds one."""
    if row["state"] == "retired" or row["identity"]["is_masked"]:
        return False
    t = triage_of(row)
    if t is None or t["payload"] is None or t["payload"]["decision"] != "pending":
        return False
    return ((t["status"], t["error_class"]) == ("skipped", "not_needed")
            and primary_role_ok(row, wants, html_primary=html_primary))


def chain_walk(store_root: str | Path, chain: Chain) -> list[Path]:
    """The ``documents`` chain's files newest to oldest: the pinned version (head to the newest
    full file), then on along ``base`` through the directory's other recorded files (the bases of
    full files, superseded versions; from the run records, D345, each downloaded when absent)
    until a full without a base, a base no record of the directory names, or a file that is gone."""
    from stack.store.index import store_index

    out = [f.path for f in reversed(chain.files)]
    idx = store_index(store_root)
    by_name = {Path(o["path"]).name: o for _, _, o in idx.dir_outputs(chain.dir)} if chain.dir else {}
    seen = {f.name for f in chain.files}
    base = chain.files[0].base
    while base and base not in seen and base in by_name:
        o = by_name[base]
        try:
            out.append(idx.fetch_output(o))
        except FileNotFoundError:
            break  # gone from the store (neither local nor remote): the walk ends there
        seen.add(base)
        base = str(o.get("base", ""))
    return out


def standings(store_root: str | Path, chain: Chain, doc_ids: Iterable[str]) -> dict[str, Row]:
    """``doc_id`` → its newest decided triage annotation (:func:`is_decided`) in the files of
    :func:`chain_walk`, newest first; reads only ``doc_id`` and ``annotations`` of each file and
    stops when every ``doc_id`` has one. A ``doc_id`` with none is left out."""
    want = set(doc_ids)
    out: dict[str, Row] = {}
    if not want:
        return out
    for path in chain_walk(store_root, chain):
        tbl = read_family(path, ["doc_id", "annotations"])[0]  # at the current format (D295)
        tbl = tbl.filter(pc.is_in(tbl["doc_id"], value_set=pa.array(sorted(want), pa.string())))
        for r in tbl.to_pylist():
            t = (r["annotations"] or {}).get(WORKFLOW)
            if t is not None and is_decided(t):
                out[r["doc_id"]] = dict(t)
                want.discard(r["doc_id"])
        if not want:
            break
    return out


def merged_standing(own: Mapping[str, Any] | None, former: Iterable[Mapping[str, Any] | None]) -> Row | None:
    """Arm 7's standing of a document with ``identity.former_doc_ids`` (D263; R3-4): ``own`` is
    its own standing (current decided annotation, else :func:`standings`), ``former`` the
    standings of its former ids, in ``former_doc_ids`` order. Any ``fetch`` wins: the first
    ``fetch`` (own before former) with ``wanted_by`` the union of every ``fetch``'s; else the first
    ``skip``; ``None`` when none is decided. Gather keeps the survivor's own annotations on a merge
    and the absorbed row's text arrives with its content (:mod:`workflows.gather.carry`), so
    without this the survivor's ``skip``, decided on its pre-merge fields, would stand for good."""
    cands = [t for t in (own, *former) if is_decided(t)]
    fetches = [t for t in cands if t is not None and t["payload"]["decision"] == "fetch"]
    if not fetches:
        return dict(cands[0]) if cands else None
    first = fetches[0]
    wanted = sorted({w for t in fetches for w in t["payload"]["wanted_by"] or []})
    return {**first, "payload": {**first["payload"], "wanted_by": wanted}}


# ---------------------------------------------------------------- answers
def question_row(q: Question, status: str, error_class: str | None = None) -> Row:
    """A ``QUESTION`` with no answer (A.2)."""
    return {"question": q.name, "status": status, "error_class": error_class, "verdict": None,
            "confidence": None, "evidence_id": None, "config_digest": q.config_digest}


def is_signal(a: Mapping[str, Any] | None) -> bool:
    """Whether a stored ``QUESTION`` is a signal with a verdict (module docstring, Signals): a
    re-derived fill that records an ``ok`` answer."""
    return a is not None and (a["status"], a["error_class"]) in REDERIVED and a.get("verdict") is not None


def signal_row(answer: Mapping[str, Any], fill: str) -> Row:
    """The stored form of ``answer`` on a row outside the question scope (module docstring,
    Signals): ``skipped[<fill>]`` with the answer's ``verdict`` and ``confidence`` when it is
    ``ok``, its ``evidence_id`` whatever its status, its ``config_digest``."""
    ok_ = answer["status"] == "ok"
    return {"question": answer["question"], "status": "skipped", "error_class": fill,
            "verdict": answer["verdict"] if ok_ else None, "confidence": answer["confidence"] if ok_ else None,
            "evidence_id": answer.get("evidence_id"), "config_digest": answer["config_digest"]}


def verdict_of(a: Mapping[str, Any] | None) -> str | None:
    """The verdict a stored ``QUESTION`` records: an ``ok`` answer's or a signal's, else ``None``."""
    if a is None:
        return None
    return a.get("verdict") if a["status"] == "ok" or is_signal(a) else None


def accepted_answers(line: StageLine | None, qs: Sequence[Question]) -> dict[str, tuple[str, ...]]:
    """Per question of ``qs``, the earlier question digests the triage line accepts (module docstring, Accepted
    answers); questions without any are left out."""
    if line is None:
        return {}
    return {q.name: acc for q in qs if (acc := line.accepted_configs(q.name))}


def is_accepted(a: Mapping[str, Any], q: Question, accepted: Mapping[str, Sequence[str]]) -> bool:
    """Whether the stored ``QUESTION`` ``a`` of question ``q`` is an accepted answer: of a digest the line accepts for
    the question, and ``ok`` or a signal (module docstring, Accepted answers)."""
    return (a["config_digest"] != q.config_digest and a["config_digest"] in accepted.get(q.name, ())
            and (a["status"] == "ok" or is_signal(a)))


def carried_answers(row: Mapping[str, Any], qs: Sequence[Question],
                    accepted: Mapping[str, Sequence[str]] | None = None) -> dict[str, Row]:
    """The row's reusable ``QUESTION`` answers by question name (module docstring); a signal is
    carried as the ``ok`` answer it records; with ``accepted`` (:func:`accepted_answers`) an accepted answer is
    carried too, under its own digest."""
    t = triage_of(row)
    if t is None or t["basis_digest"] != row["metadata"]["fields_digest"] or t["payload"] is None:
        return {}
    by_name = {q.name: q for q in qs}
    out: dict[str, Row] = {}
    for a in t["payload"]["questions"] or []:
        q = by_name.get(a["question"])
        if q is None or (q.config_digest != a["config_digest"] and not is_accepted(a, q, accepted or {})):
            continue
        if is_signal(a):
            out[a["question"]] = {**a, "status": "ok", "error_class": None}
            continue
        if (a["status"], a["error_class"]) in REDERIVED:
            continue
        out[a["question"]] = dict(a)
    return out


def can_ask(asker: Any, row: Mapping[str, Any]) -> bool:
    """Whether ``asker`` would send a question for ``row`` (a gate-view row): its ``can_ask``
    method when it has one (the LLM asker: an abstract or excerpt, or a title under
    ``title_basis``), else ``True``."""
    f = getattr(asker, "can_ask", None)
    return True if f is None else bool(f(row))


def answer_due(row: Mapping[str, Any], qs: Sequence[Question], evidence_by_id: Mapping[str, Mapping[str, Any]],
               now: dt.datetime, *, askable: Callable[[Mapping[str, Any]], bool], have_prior: bool = True,
               accepted: Mapping[str, Sequence[str]] | None = None) -> bool:
    """D724's due (module docstring, Signals): an unmasked, not ``retired`` row with a triage
    annotation on its current basis (else :func:`in_scope` has it) that lacks a settled answer for
    some question: no row of the current question digest, or a fill that is not a settled signal.
    A fill with a verdict is settled; a fill with an ``evidence_id`` is settled when that evidence
    is in ``evidence_by_id`` (the prior rows) and the retry rule carries it (a due
    ``failed_transient`` is not, nor, R2-1 as :func:`question_due`, evidence missing from the prior;
    ``have_prior`` false: the run has no prior evidence, and such a fill counts as settled rather
    than re-asking every signal); a fill with neither is settled only when ``askable(row)`` is false
    (no basis to ask on). A real answer, final or open, is never this rule's (:func:`question_due`
    has the open ones of ``pending`` rows). An accepted answer (``accepted``, module docstring) is settled."""
    from stack.annotate.evidence import prior_decision

    if row["state"] == "retired" or row["identity"]["is_masked"]:
        return False
    t = triage_of(row)
    if t is None or t["payload"] is None or t["basis_digest"] != row["metadata"]["fields_digest"]:
        return False
    stored = {a["question"]: a for a in t["payload"]["questions"] or []}
    for q in qs:
        a = stored.get(q.name)
        if a is not None and accepted and is_accepted(a, q, accepted):
            continue
        if a is not None and a["config_digest"] == q.config_digest:
            if (a["status"], a["error_class"]) not in REDERIVED or a.get("verdict") is not None:
                continue
            if a.get("evidence_id") is not None:
                if not have_prior:
                    continue
                ev = evidence_by_id.get(a["evidence_id"])
                if ev is not None and prior_decision(ev, now) != "ask":
                    continue
                return True
        if askable(row):
            return True
    return False


def question_due(row: Mapping[str, Any], qs: Sequence[Question],
                 evidence_by_id: Mapping[str, Mapping[str, Any]], now: dt.datetime) -> bool:
    """§3.3's "due" for triage (``workflows/triage/main.py``; D161 (C)): an unmasked, not
    ``retired`` row whose current triage decision is ``pending`` with a carried answer (current
    basis and question digest, :func:`carried_answers`) that is ``not_attempted`` (never sent:
    budget, halt, row limit) or ``failed_transient`` whose evidence (``evidence_by_id``, the
    prior rows) the annotate retry rule would ask now (backoff passed, under the cap). A
    transient answer whose evidence is **not** in the prior is due (R2-1, round 5): the run that
    wrote that evidence never reached a prior (its build failed, or a stand-alone run without
    its ``triage_evidence`` directory as ``--in``), and without this rule the row would stay ``pending`` for good. The asker then
    finds no prior evidence either and asks it as a first attempt (``attempts`` restart, as
    annotate's ``prior_decision(None) == "ask"``); its new evidence is in the run's cumulative
    file, so the backoff and the cap hold again from there."""
    from stack.annotate.evidence import prior_decision

    if row["state"] == "retired" or row["identity"]["is_masked"]:
        return False
    t = triage_of(row)
    if t is None or t["payload"] is None or t["payload"]["decision"] != "pending":
        return False
    for a in carried_answers(row, qs).values():
        if a["status"] == "not_attempted":
            return True
        if a["status"] == "failed_transient":
            ev = evidence_by_id.get(a["evidence_id"]) if a["evidence_id"] else None
            if ev is None or prior_decision(ev, now) == "ask":
                return True
    return False


def clock() -> dt.datetime:
    """Now, as the LLM asker sees it: the annotate framework's ``CLOCK`` seam when a test set it
    (``stack.annotate.stage.CLOCK``), else UTC now; so "due" means the same in scope and asker."""
    from stack.annotate import stage as annotate_stage

    return annotate_stage.CLOCK() if annotate_stage.CLOCK is not None else dt.datetime.now(dt.UTC)


def is_final(answer: Mapping[str, Any] | None, *, no_basis_final: bool = True) -> bool:
    """Whether a carried answer is final (:data:`FINAL`; the asker is not asked again about it).
    ``no_basis_final=False``: a ``skipped[no_basis]`` answer is not, for an asker that can ask such a row
    (the triage option ``title_basis``, D527; :func:`asks_without_basis`)."""
    if answer is None:
        return False
    st, ec = answer["status"], answer["error_class"]
    if not no_basis_final and (st, ec) == ("skipped", "no_basis"):
        return False
    return (st, None) in FINAL or (st, ec) in FINAL


def asks_without_basis(asker: Any) -> bool:
    """Whether ``asker`` asks rows that have no abstract and no excerpt (its ``asks_without_basis``
    attribute; the LLM asker under ``title_basis: true``, D527). Such an asker is asked again about a
    carried ``skipped[no_basis]``."""
    return bool(getattr(asker, "asks_without_basis", False))


# ---------------------------------------------------------------- the decision table
@dataclass(frozen=True)
class Decision:
    decision: str
    status: str
    error_class: str | None
    wanted_by: tuple[str, ...]
    origin: str
    arm: int


def decide(cls_: Sequence[Clause], hits: Iterable[int], answers: Mapping[str, Mapping[str, Any]],
           *, asked_origin: str = ASKED_ORIGIN, standing: Mapping[str, Any] | None = None) -> Decision:
    """§3.1's seven arms, first match wins (module docstring). ``hits`` = indices of the matching
    ``sql:`` clauses; ``answers`` = one ``QUESTION`` per question name of the gate (a missing
    name is treated as ``skipped[not_requested]``); ``standing`` = the row's standing
    (:func:`standings`), passed only for a row whose primary role is ``ok`` (arm 7)."""
    hit = set(hits)
    positive: set[str] = set()
    answered_positive = False
    all_negative = True
    used_answer = False
    for c in cls_:
        if c.sql is not None:
            if c.index in hit:
                positive.add(c.consumer)
            continue
        assert c.question is not None
        a = answers.get(c.question)
        if a is not None and a["status"] == "ok":
            used_answer = True
            if a["verdict"] in c.fetch_on:
                positive.add(c.consumer)
                answered_positive = True
        else:
            all_negative = False
    if positive:
        sql_only = not answered_positive
        return Decision("fetch", "ok", None, tuple(sorted(positive)),
                        RULE_ORIGIN if sql_only else asked_origin, 1)
    if all_negative:
        return Decision("skip", "ok", None, (), asked_origin if used_answer else RULE_ORIGIN, 2)
    names = list(dict.fromkeys(c.question for c in cls_ if c.question is not None))
    unresolved = [answers.get(n) or {"status": "skipped", "error_class": "not_requested"}
                  for n in names]
    unresolved = [a for a in unresolved if a["status"] != "ok"]
    if all((a["status"], a["error_class"]) == ("skipped", "no_basis") for a in unresolved):
        return Decision("pending", "skipped", "no_basis", (), RULE_ORIGIN, 3)
    open_ = [a for a in unresolved if a["status"] in RESOLVABLE]
    if not open_ and any(a["status"] in TERMINAL for a in unresolved):
        return Decision("pending", "skipped", "gate_failed", (), RULE_ORIGIN, 4)
    if open_:
        return Decision("pending", str(open_[0]["status"]), open_[0]["error_class"], (), RULE_ORIGIN, 5)
    carried = _carried_standing(cls_, names, answers, standing)
    if carried is not None:
        return carried
    return Decision("pending", "skipped", "not_needed", (), RULE_ORIGIN, 6)


def _carried_standing(cls_: Sequence[Clause], names: Sequence[str], answers: Mapping[str, Mapping[str, Any]],
                      standing: Mapping[str, Any] | None) -> Decision | None:
    """Arm 7 (module docstring) on a row that reached arm 6, or ``None``."""
    if not is_decided(standing):
        return None
    assert standing is not None
    fills = [answers.get(n) or {"status": "skipped", "error_class": "not_requested"} for n in names]
    if any((a["status"], a["error_class"]) not in REDERIVED for a in fills):
        return None
    p = standing["payload"]
    consumers = {c.consumer for c in cls_}
    wanted = tuple(sorted(w for w in p["wanted_by"] or [] if w in consumers))
    if p["decision"] == "fetch" and not wanted:
        return None
    return Decision(str(p["decision"]), "ok", None, wanted if p["decision"] == "fetch" else (),
                    str(standing["origin"]), 7)


def annotation(d: Decision, qs_rows: Sequence[Row], *, basis_digest: str, config_digest: str,
               decided_at: dt.datetime) -> Row:
    """The ``ANN<TRIAGE>`` of a decision (A.4; S9: ``basis_kind = 'fields'``, ``basis_digest`` =
    the row's ``fields_digest``)."""
    return {"workflow": WORKFLOW, "version": payload(WORKFLOW).version, "status": d.status,
            "error_class": d.error_class, "basis_kind": BASIS_KIND, "basis_digest": basis_digest,
            "config_digest": config_digest, "decided_at": decided_at, "evidence_id": None,
            "origin": d.origin, "is_basis_current": True,
            "payload": {"decision": d.decision, "wanted_by": list(d.wanted_by),
                        "questions": list(qs_rows)}}


# ---------------------------------------------------------------- the asker seam
@dataclass
class AskResult:
    """What an asker returns: ``answers[(doc_id, question)] = QUESTION`` and the
    ``triage_evidence`` rows of the calls it made: Python rows, or a table in the family's schema (the LLM asker's
    cumulative file, D780)."""

    answers: dict[tuple[str, str], Row] = field(default_factory=dict)
    evidence: list[Row] | pa.Table = field(default_factory=list)


class Asker(Protocol):
    """The asker protocol (module docstring)."""

    name: str
    origin: str

    def ask(self, rows: list[Row], questions: list[Question], ctx: StageContext) -> AskResult: ...


#: ``(plan, stage_line) -> Asker | None``; a test seam (a fake asker, or ``None`` for no asker).
ASKER_HOOK: Callable[[Plan, StageLine], Asker | None] | None = None


def asker_for(plan: Plan, stage_line: StageLine) -> Asker | None:
    """The run's asker: ``ASKER_HOOK(plan, stage_line)`` when the hook is set; else, when the gate
    has questions, the LLM asker (``workflows/triage/ask.py``, T4.2); else ``None``."""
    if ASKER_HOOK is not None:
        return ASKER_HOOK(plan, stage_line)
    if not questions(plan):
        return None
    from workflows.triage.ask import llm_asker

    return llm_asker(plan, stage_line)


# ---------------------------------------------------------------- recompute (X14)
def recompute(rows: Sequence[Mapping[str, Any]], gate: Gate, groups: Mapping[str, str], *,
              carry: bool = False, wants: Mapping[str, Sequence[str]] | None = None,
              html_primary: bool = False) -> dict[str, Decision]:
    """X14's recompute: per row, :func:`decide` over the ``sql:`` clauses on the row's gate view
    and the row's own ``questions[]`` answers (by name, as carried). Rows without a triage
    annotation are left out. ``carry``: a stored decided annotation is the row's standing (arm 7)
    when its primary role is ``ok`` under ``wants`` and ``html_primary`` (``wants=None``: that test is skipped), so a
    carried decision recomputes to itself, its ``wanted_by`` filtered to the gate's consumers.
    A row in the question scope under ``wants`` (no ``sql:`` hit, primary role not ``ok``) counts a
    stored signal as the ``ok`` answer it records, as the triage stage carries it there
    (:func:`carried_answers`): its primary role stopped being ``ok`` after triage stored the answer
    as a signal, and triage would decide on that answer now (D785). ``wants=None``: signals stay fills."""
    rows = [r for r in rows if triage_of(r) is not None]
    if not rows:
        return {}
    view = gate_view(pa.Table.from_pylist(list(rows), schema=family("documents").schema), groups)
    cls_ = clauses(gate)
    hits = sql_hits(view, cls_)
    out: dict[str, Decision] = {}
    for r in rows:
        t = triage_of(r)
        assert t is not None
        hit = hits.get(r["doc_id"], set())
        prim = wants is None or primary_role_ok(r, wants, html_primary=html_primary)
        answers = {a["question"]: ({**a, "status": "ok", "error_class": None}
                                   if not prim and not hit and is_signal(a) else a)
                   for a in (t["payload"] or {}).get("questions") or []}
        standing = t if carry and is_decided(t) and prim else None
        out[r["doc_id"]] = decide(cls_, hit, answers, standing=standing)
    return out


def resolve_plan(ref: str) -> Plan | None:
    """A build record's ``plan``: a path to a plan file, or a plan name resolved to
    ``configs/plans/<name>.yaml`` under the current directory, then the repository; ``None``
    when no file exists."""
    p = Path(ref)
    if p.suffix in (".yaml", ".yml") or len(p.parts) > 1:
        cands = [p]
    else:
        repo = Path(__file__).resolve().parents[1]
        cands = [Path("configs/plans") / f"{ref}.yaml", repo / "configs" / "plans" / f"{ref}.yaml"]
    for c in cands:
        if c.exists():
            return load_plan(c)
    return None


__all__ = [
    "ASKED_ORIGIN", "ASKER_HOOK", "DECIDED", "AskResult", "Asker", "Clause", "Decision", "GateError",
    "Question", "accepted_answers", "annotation", "answer_due", "asker_for", "can_ask", "carried_answers", "chain_walk",
    "is_accepted",
    "channel_groups", "clauses", "is_signal", "signal_row", "verdict_of",
    "HTML_PRIMARY_MODES", "clock", "decide", "gate_columns", "gate_digest", "gate_view", "html_is_primary",
    "in_question_scope", "in_scope",
    "asks_without_basis", "is_decided", "is_final", "load_gate", "primary_role_ok", "question_due", "question_row", "questions",
    "recompute", "resolve_plan", "sql_hits", "standing_due", "standings",
    "store_gate", "triage_config_digest",
]
