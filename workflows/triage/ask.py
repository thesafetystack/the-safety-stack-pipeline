"""Triage questions: the LLM asker for the gate's ``question:`` clauses (T4.2; §3.1 "The triage
gate", A.2 ``QUESTION``, A.5 ``triage_evidence``; D19, D22, D23, D36, D37).

:func:`stack.gate.asker_for` builds it (:func:`llm_asker`) when the plan's gate has question
clauses and no test hook is set; ``workflows/triage/main.py`` hands it the scoped rows that lack a
final carried answer (every scoped row since D724, not only the question scope's) and turns its
answers into decisions (arms 1-6 are :func:`stack.gate.decide`'s) or, outside the question scope,
signals (:func:`stack.gate.signal_row`). This module only answers questions.

**Prompt pin.** A question names a plan prompt key; ``plan.prompts[<key>]`` is the pin:
``name`` (required; selects the template ``workflows/triage/<name>.md`` and is the evidence's
``prompt_name``), ``model`` (default: the role's model), ``role`` (default :data:`ROLE`, an
``llm.yaml`` role: ``max_tokens``, fallbacks), ``template_digest`` (optional; a pin naming a
digest the template does not have is refused before any call). Other keys are refused. The pin
is in the question's ``config_digest`` (:func:`stack.gate.questions`), so a new model or prompt
name re-asks; a template edited under the same name does not (only the asker ``name`` changes,
which re-decides rows without calls): a changed prompt is a new file with a new name.

**Record versions** (D656; :data:`SPECS`, one row per template file: its record version and, for a
question template, its title variant). The template name also selects the record it is sent: version 1
(:func:`document_text`, :func:`title_text`; every name up to ``safety-relevance-v3`` /
``safety-relevance-title-v1``) or version 2 (:func:`document_text_v2`,
:func:`title_text_v2`: version 1 plus, after ``Authors``, ``Published: <year>`` of
``metadata.published_at`` and ``Source: <channels>`` of ``identity.channels``, each left out when the
row has none; ``safety-relevance-v4``, ``safety-relevance-title-v2`` and their ``-p4`` variants). The
rendered record is in no digest that decides re-asking (the work key hashes ``fields_digest`` and the
question's ``config_digest``), so a changed record is a new name, like a changed prompt, and an older
name keeps its record byte for byte. A template file whose name is not listed is refused before any
call (it would otherwise get some record silently).

**Basis ``fields``** (the triage annotation's, S9): title, authors, abstract and the anchor's
in-band excerpt (the ``text`` blob of the gate view's ``inband_text_sha256``; ``fields_digest``
covers that digest, §2.7). No abstract and no excerpt → ``skipped[no_basis]`` (arm 3), no call,
no evidence. A missing excerpt blob fails the run before any call (the store is inconsistent).

**Title basis** (D527; stage option ``title_basis: true``, default off): such a row, when it has a
title, is asked instead with the question's title variant (:attr:`Spec.title_variant` of the pin's
template name: a file beside the triage prompts; same role and model; a question
without a variant is refused before any call) on :func:`title_text`: title, authors, their raw
affiliations, the OpenAlex primary topic, topics and keywords from a live OpenAlex member's
``provider_record`` (D522) when there is one, and the labels of the queries that found its members
(``sources.metadata.matched_by``, read from the ``sources`` inputs the build passes when the option
is on; none given, none shown). The evidence row's ``basis`` is ``title`` (``fields`` otherwise). A
carried ``skipped[no_basis]`` is then not final (:func:`stack.gate.asks_without_basis`), so turning
the option on asks the rows an earlier run skipped. A later abstract or excerpt changes
``fields_digest``, so the row is asked again with the normal prompt; evidence of the other basis, or
of another title variant, under the same work key is not carried (a fresh first attempt that
supersedes it). The variants and the option enter the asker ``name``, so the triage
``config_digest`` moves with them; without the option the name is unchanged.

**Per ``(row, question)``** the work key is ``H([anchor source_id, fields_digest,
question.config_digest, ''])`` (D19; :func:`stack.annotate.basis.work_digest`), and the selected
prior evidence of that key decides (:func:`stack.annotate.evidence.prior_decision`, the
annotate rule): no evidence, or a due ``failed_transient`` under the cap → ask; anything else →
carry, no call. An ask goes through the client (ledger reservation per attempt, D36; response
cache) and gives one ``triage_evidence`` row (EVI + ``question, verdict, confidence, reasoning``
+ T2, built by :func:`stack.annotate.evidence.evidence_row`):

* a schema-valid answer → ``ok`` with the verdict and confidence;
* :class:`~stack.llm.BudgetExceeded` (the stage's ``budget.max_usd``, else ``llm.yaml`` ``budget.per_run_usd``) →
  that pair and every later one ``not_attempted[budget]``, never sent, no evidence;
* :class:`~stack.llm.FatalLlmError` → its evidence row; every later pair ``not_attempted[halted]``;
  an auth/credit stop (:func:`stack.annotate.stage.auth_stop`) writes no evidence row and charges
  no attempt: its pairs are ``not_attempted[halted]`` too (D430);
* any other :class:`~stack.llm.LlmError` → its evidence row with the error's ``(status,
  error_class)``: ``failed_transient`` is due again at the next run after the first attempt, then
  backs off ``1h·2^(attempts-1)`` (``retry_after``, :func:`stack.annotate.evidence.backoff`) and turns
  ``quarantined[cap_reached]`` on attempt 3 (arm 5, then arm 4); ``failed_permanent`` (schema-
  invalid after the client's retries, which is how a prose refusal ends; egress refusal; too
  large) is arm 4.

The ``QUESTION`` answer is read off the evidence row (``evidence_id`` set). The asker returns the
**cumulative** evidence (every prior row, then this run's), as the annotate stages do
(D129), so the run's ``triage_evidence`` file is a chain of one that keeps the history.

Pairs are admitted in the order of the rows given, then plan question order (the triage stage gives
the question scope's rows first, then the others, each in ``doc_id`` order: D724, so a budget or
``max_rows`` stop cuts the signals before any answer that decides); the run heartbeats after each answered
call. ``options.workers`` (default :data:`stack.annotate.stage.DEFAULT_WORKERS`, at most :data:`stack.annotate.stage.MAX_WORKERS`; annotate's
semantics) asks that many pairs at once from a thread pool over one client: admission (halt,
budget, limit) is decided in the calling thread, in that order, before a pair is submitted (the
ledger reservation still precedes each call, inside the client); pairs in flight when the run
halts or the budget trips finish and are kept; the evidence rows are assembled in admission order
afterwards, so the output is the same as one worker's for the same answers. ``workers`` is not in
the triage ``config_digest`` (``workflows/triage/main.py`` drops it from the stage line's digest,
as annotate's ``config_digest`` leaves ``options`` out): changing it re-scopes nothing.
A drain (D570, D571; ``ctx.drain``) admits no further pair: the rest are ``not_attempted[stopped]``
(count ``ask.not_attempted.stopped``, build reason ``stopped``); pairs in flight get the drain's
bounded wait, then are abandoned, ``not_attempted[stopped]`` too (late answers dropped; the next run
asks them, a cached response serving it free).
The stage line's ``options.max_rows`` caps the documents sent to the model in one run (a dev-run
cap, as annotate's): the pairs of later documents are left out of the answers, which the gate
records as ``not_attempted[limit]`` with ``scope_complete = false``; a later run asks them.
**Batch mode** (``options.llm_mode: batch``; :mod:`stack.annotate.batchmode`, :mod:`stack.llm.batch`):
the client is opened before the loop and the pairs the loop would admit (the ``max_rows`` document
cap in queue order) are asked in collect passes whose requests go out as OpenRouter batches at half
price; the loop then runs unchanged as the replay pass, served from the batch outcomes. A pair the
batches left without an answer (``batch_wait_minutes`` time cap, a failed/expired batch, a submit
error) is ``not_attempted[unanswered]`` (count ``ask.not_attempted.unanswered``; no evidence, the
next run asks it again); counts ``batch.*``. ``llm_mode`` and the ``batch_*`` options are in
:data:`UNDIGESTED_OPTIONS`.
Test seams are the annotate framework's (:data:`stack.annotate.stage.TRANSPORT_HOOK`, ``CLOCK``,
``SLEEP``; the batch driver's clock :data:`stack.annotate.batchmode.CLOCK`), so one fake serves both.
"""

from __future__ import annotations

import datetime as dt
import functools
import time
from collections.abc import Iterable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pyarrow as pa
import pyarrow.compute as pc

from stack import gate
from stack.annotate import batchmode
from stack.annotate import stage as annotate_stage
from stack.annotate.basis import basis_of, work_digest
from stack.annotate.evidence import (
    TRIAGE_EVIDENCE_SPEC,
    by_work,
    evidence_row,
    prior_decision,
)
from stack.annotate.provenance import record_lines as provenance_lines
from stack.checks.digests import H
from stack.core.hashes import template_hash
from stack.llm import (
    BatchUnanswered,
    BudgetScope,
    FatalLlmError,
    LicenseClass,
    LlmClient,
    LlmError,
    LlmResult,
    open_client,
)
from stack.schemas.families import family
from stack.stage_runner import DrainPool, StageError
from stack.store.chain import read_view

if TYPE_CHECKING:
    from stack.config import LlmConfig
    from stack.llm.batch import BatchOptions
    from stack.plan import Plan, StageLine
    from stack.stage_runner import StageContext

HERE = Path(__file__).resolve().parent
#: The ``llm.yaml`` role a pin uses unless it names another (``openai/gpt-6-luna``, 512 output tokens; D201).
ROLE = "classify"
PIN_KEYS = frozenset({"name", "model", "role", "template_digest"})
#: Stage-line ``options`` that only steer how the run asks (not what): kept out of the triage
#: ``config_digest`` (``workflows/triage/main.py``), as annotate keeps every option out of its own.
#: The ``options`` keys :func:`llm_asker` reads (D434): ``max_rows``, ``workers``, batch mode.
OPTIONS: frozenset[str] = frozenset({"max_rows", "workers", "title_basis"}) | batchmode.OPTIONS
UNDIGESTED_OPTIONS = frozenset({"workers", "llm_mode", "batch_max_requests", "batch_max_mb", "batch_min_requests",
                                "batch_wait_minutes"})
EVIDENCE = "triage_evidence"
VERDICTS = ("relevant", "borderline", "not_relevant")
#: The model's answer (A.5's ``verdict, confidence, reasoning``).
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": list(VERDICTS)},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reasoning": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
    "additionalProperties": False,
}
#: Caps on what one record sends (an abstract is rarely longer; the excerpt is a lead, not the text).
MAX_ABSTRACT_CHARS = 6_000
MAX_EXCERPT_CHARS = 3_000
MAX_AUTHORS = 12


@dataclass(frozen=True)
class Spec:
    """What a template name selects (module docstring "Record versions"): one row of :data:`SPECS`."""

    #: The record it is sent: 1 (:func:`document_text`, :func:`title_text`) or 2 (their ``_v2``).
    record_version: int
    #: Its title variant's template name (D527, module docstring "Title basis"), or None.
    title_variant: str | None = None


#: Every template file of this directory, the title variants included (a test checks both directions), and what
#: it selects. A new prompt version is one row here (and its file). ``-p4``: v4 / title-v2 plus the
#: technique-provenance clause (D657; no plan pins them until an independent evaluation promotes them).
SPECS: dict[str, Spec] = {
    "safety-relevance-v1": Spec(1),
    "safety-relevance-v2": Spec(1),
    "safety-relevance-v3": Spec(1, title_variant="safety-relevance-title-v1"),
    "safety-relevance-title-v1": Spec(1),
    "safety-relevance-v4": Spec(2, title_variant="safety-relevance-title-v2"),
    "safety-relevance-title-v2": Spec(2),
    "safety-relevance-v4-p4": Spec(2, title_variant="safety-relevance-title-v2-p4"),
    "safety-relevance-title-v2-p4": Spec(2),
}
#: The evidence ``basis`` values (``triage_evidence.basis``, D527).
BASIS_FIELDS, BASIS_TITLE = "fields", "title"
#: Channels whose ``provider_record`` is an OpenAlex work (topics, keywords).
TOPIC_CHANNELS = frozenset({"openalex"})
#: Caps on the title-basis record's lists.
MAX_AFFILIATIONS = 12
MAX_TOPICS = 8
MAX_KEYWORDS = 12
MAX_LABELS = 12
#: Threads reading the queue's excerpt blobs before the first call (a fresh container downloads each
#: from the remote; read one by one that was tens of minutes at 50k rows, S25-94).
EXCERPT_READERS = 8
#: ``triage_evidence``'s payload columns as a registry-shaped spec, so the annotate evidence
#: builder (EVI, T2, attempts, backoff, ``result_digest``) makes its rows (and V8 checks them).
EVIDENCE_SPEC = TRIAGE_EVIDENCE_SPEC

Row = dict[str, Any]


@dataclass(frozen=True)
class QuestionPrompt:
    """A gate question resolved against ``llm.yaml``: its template, role, model, digest."""

    question: gate.Question
    name: str
    template: str
    role: str
    model: str
    max_tokens: int
    temperature: float | None
    template_digest: str
    reasoning_effort: str | None = None


def resolve(q: gate.Question, llm: LlmConfig) -> QuestionPrompt:
    """The question's prompt from its pin (module docstring); a bad pin raises
    :class:`stack.gate.GateError` before any call."""
    pin = dict(q.pin)
    where = f"gate question {q.name!r}: plan.prompts[{q.prompt!r}]"
    extra = sorted(set(pin) - PIN_KEYS)
    if extra:
        raise gate.GateError(f"{where} keys {extra} not in {sorted(PIN_KEYS)}")
    name = pin.get("name")
    if not isinstance(name, str) or not name:
        raise gate.GateError(f"{where} needs a `name` (the template workflows/triage/<name>.md)")
    path = HERE / f"{name}.md"
    if not path.is_file():
        raise gate.GateError(f"{where}: no template {path}")
    _registered(name, where)
    role = str(pin.get("role") or ROLE)
    if role not in llm.roles:
        raise gate.GateError(f"{where}: role {role!r} is not in llm.yaml roles")
    rc = llm.roles[role]
    model = str(pin.get("model") or rc.model)
    template = path.read_text("utf-8")
    params: dict[str, Any] = {"model": model, "max_tokens": rc.max_tokens}
    if rc.temperature is not None:
        params["temperature"] = rc.temperature
    if rc.reasoning_effort is not None:
        params["reasoning_effort"] = rc.reasoning_effort
    digest = template_hash(template, SCHEMA, params)
    want = pin.get("template_digest")
    if want is not None and want != digest:
        raise gate.GateError(f"{where} pins template_digest {want} but {path.name} with "
                             f"{model} has {digest}")
    return QuestionPrompt(q, name, template, role, model, rc.max_tokens, rc.temperature, digest, rc.reasoning_effort)


def resolve_title(qp: QuestionPrompt, llm: LlmConfig) -> QuestionPrompt:
    """The title variant of a resolved question prompt (D527, :attr:`Spec.title_variant`): same question,
    role and model, the variant's template and digest. Raises :class:`stack.gate.GateError` when the
    question's template has no variant or its file is missing (before any call)."""
    variant = SPECS[qp.name].title_variant
    where = f"gate question {qp.question.name!r}: title_basis"
    if variant is None:
        known = sorted(n for n, s in SPECS.items() if s.title_variant)
        raise gate.GateError(f"{where}: template {qp.name!r} has no title variant (known: {known})")
    path = HERE / f"{variant}.md"
    if not path.is_file():
        raise gate.GateError(f"{where}: no template {path}")
    _registered(variant, where)
    template = path.read_text("utf-8")
    params: dict[str, Any] = {"model": qp.model, "max_tokens": qp.max_tokens}
    if qp.temperature is not None:
        params["temperature"] = qp.temperature
    if qp.reasoning_effort is not None:
        params["reasoning_effort"] = qp.reasoning_effort
    return QuestionPrompt(qp.question, variant, template, qp.role, qp.model, qp.max_tokens, qp.temperature,
                          template_hash(template, SCHEMA, params), qp.reasoning_effort)


def _registered(name: str, where: str) -> None:
    """Refuse a template whose name has no :data:`SPECS` row."""
    if name not in SPECS:
        raise gate.GateError(f"{where}: template {name!r} is not registered: add its row to SPECS in "
                             f"workflows/triage/ask.py (the record it is sent)")


def asker_name(prompts: Sequence[QuestionPrompt], titles: Sequence[QuestionPrompt] = ()) -> str:
    """``llm:<16 hex>``: ``H`` over each question's ``(name, prompt, template_digest, role,
    model)`` in plan order, then (D527, only under ``title_basis``) each title variant's ``("title",
    question, name, template_digest)``. Part of the triage ``config_digest``
    (:func:`stack.gate.triage_config_digest`)."""
    items: list[list[Any]] = [[p.question.name, p.question.prompt, p.template_digest, p.role, p.model]
                              for p in prompts]
    items += [["title", t.question.name, t.name, t.template_digest] for t in titles]
    return "llm:" + H(items)[:16]


def document_text(row: Mapping[str, Any], excerpt: str | None) -> str:
    """The per-call user message: the record of a gate-view row (module docstring), version 1."""
    return "\n\n".join(_document_parts(row, excerpt))


def document_text_v2(row: Mapping[str, Any], excerpt: str | None) -> str:
    """Record version 2 (module docstring): :func:`document_text` plus :func:`provenance_lines` after
    ``Authors``."""
    parts = _document_parts(row, excerpt)
    return "\n\n".join([*parts[:2], *provenance_lines(row), *parts[2:]])


def record_text(name: str, row: Mapping[str, Any], excerpt: str | None) -> str:
    """The record template ``name`` is sent for a row with a basis (:attr:`Spec.record_version`)."""
    return document_text(row, excerpt) if SPECS[name].record_version == 1 else document_text_v2(row, excerpt)


def title_record_text(name: str, row: Mapping[str, Any], subj: Mapping[str, Any] | None,
                      labels: Sequence[str]) -> str:
    """The record title variant ``name`` is sent (:attr:`Spec.record_version`)."""
    return title_text(row, subj, labels) if SPECS[name].record_version == 1 else title_text_v2(row, subj, labels)


def _document_parts(row: Mapping[str, Any], excerpt: str | None) -> list[str]:
    md = row["metadata"]
    names = [a["name"] for a in md.get("authors") or [] if a.get("name")]
    authors = ", ".join(names[:MAX_AUTHORS]) + (f" (+{len(names) - MAX_AUTHORS} more)"
                                                if len(names) > MAX_AUTHORS else "")
    parts = [f"Title: {md.get('title') or 'Unknown'}", f"Authors: {authors or 'Unknown'}"]
    if md.get("abstract"):
        parts.append("Abstract:\n" + _cap(md["abstract"], MAX_ABSTRACT_CHARS))
    if excerpt:
        parts.append("Excerpt (start of the text):\n" + _cap(excerpt, MAX_EXCERPT_CHARS))
    return parts


def has_title(row: Mapping[str, Any]) -> bool:
    """A title-basis ask is possible (D527): the row has a non-empty title."""
    return bool(str(row["metadata"].get("title") or "").strip())


def subjects(record: Any) -> dict[str, Any]:
    """The subject fields of an OpenAlex work (its ``provider_record``, D522): ``primary_topic`` (``"<topic>
    (<subfield> / <field>)"``), ``topics`` and ``keywords`` (display names, in the record's order, capped);
    ``{}`` when the record has none or is not a JSON object."""
    if not isinstance(record, Mapping):
        return {}

    def name(x: Any) -> str | None:
        v = x.get("display_name") if isinstance(x, Mapping) else None
        return str(v).strip() or None if v else None

    out: dict[str, Any] = {}
    pt = record.get("primary_topic")
    if isinstance(pt, Mapping) and name(pt):
        where = " / ".join(n for n in (name(pt.get("subfield")), name(pt.get("field"))) if n)
        out["primary_topic"] = f"{name(pt)} ({where})" if where else name(pt)
    topics = [n for t in record.get("topics") or [] if (n := name(t))]
    if topics:
        out["topics"] = list(dict.fromkeys(topics))[:MAX_TOPICS]
    keywords = [n for k in record.get("keywords") or [] if (n := name(k))]
    if keywords:
        out["keywords"] = list(dict.fromkeys(keywords))[:MAX_KEYWORDS]
    return out


def title_text(row: Mapping[str, Any], subj: Mapping[str, Any] | None, labels: Sequence[str]) -> str:
    """The per-call user message of a title-basis ask (D527): title, authors, raw affiliations, a note
    that there is no abstract, the OpenAlex subjects (:func:`subjects`) and the query labels. Version 1."""
    return "\n\n".join(_title_parts(row, subj, labels))


def title_text_v2(row: Mapping[str, Any], subj: Mapping[str, Any] | None, labels: Sequence[str]) -> str:
    """Record version 2 of a title-basis ask: :func:`title_text` plus :func:`provenance_lines` after
    ``Authors``."""
    parts = _title_parts(row, subj, labels)
    return "\n\n".join([*parts[:2], *provenance_lines(row), *parts[2:]])


def _title_parts(row: Mapping[str, Any], subj: Mapping[str, Any] | None, labels: Sequence[str]) -> list[str]:
    md = row["metadata"]
    authors = md.get("authors") or []
    names = [a["name"] for a in authors if a.get("name")]
    who = ", ".join(names[:MAX_AUTHORS]) + (f" (+{len(names) - MAX_AUTHORS} more)"
                                            if len(names) > MAX_AUTHORS else "")
    parts = [f"Title: {md.get('title') or 'Unknown'}", f"Authors: {who or 'Unknown'}"]
    affs = list(dict.fromkeys(a["affiliation_raw"].strip() for a in authors if (a.get("affiliation_raw") or "").strip()))
    if affs:
        parts.append("Affiliations: " + "; ".join(affs[:MAX_AFFILIATIONS])
                     + (f" (+{len(affs) - MAX_AFFILIATIONS} more)" if len(affs) > MAX_AFFILIATIONS else ""))
    parts.append("Abstract: none (no abstract or text is available for this record)")
    subj = subj or {}
    if subj.get("primary_topic"):
        parts.append(f"Primary topic: {subj['primary_topic']}")
    if subj.get("topics"):
        parts.append("Topics: " + "; ".join(subj["topics"]))
    if subj.get("keywords"):
        parts.append("Keywords: " + "; ".join(subj["keywords"]))
    if labels:
        parts.append("Search groups that found it: " + "; ".join(list(labels)[:MAX_LABELS]))
    return parts


def topic_record_sha256(row: Mapping[str, Any]) -> str | None:
    """The ``raw.sha256`` of the ``provider_record`` of the row's first live OpenAlex member (anchor first,
    then member order), or ``None``."""
    ident = row["identity"]
    live = [m for m in ident["members"] or [] if m["is_live"] and m["channel"] in TOPIC_CHANNELS]
    live.sort(key=lambda m: m["source_id"] != ident["anchor_source_id"])
    recs = {e["source_id"]: e["raw"]["sha256"] for e in (row.get("inband") or {}).get("entries") or []
            if e["content_role"] == "provider_record" and e["raw"]}
    return next((recs[m["source_id"]] for m in live if m["source_id"] in recs), None)


def match_labels(ctx: StageContext, source_ids: Iterable[str] | None = None) -> dict[str, list[str]]:
    """``source_id`` → the labels (else query ids) of its ``metadata.matched_by``, over the run's
    ``sources`` inputs (D527; none given: ``{}``). ``source_ids``: only those sources, read as the projected
    ``source_id`` and ``metadata.matched_by`` of the rows with those keys (D780: the whole sources chains are not
    held); ``None``: every source of the cached views."""
    out: dict[str, list[str]] = {}
    want = None if source_ids is None else sorted(set(source_ids))
    for key, ch in ctx.inputs.items():
        if ch.family != "sources":
            continue
        if want is None:
            tbl = ctx.view(key).select(["source_id", "metadata"])
        elif not want:
            continue
        else:
            tbl = read_view(ch, columns=["source_id", "metadata.matched_by"], keys=want)
        for r in tbl.to_pylist():
            ms = (r["metadata"] or {}).get("matched_by") or []
            labels = [str(m.get("label") or m.get("query_id")) for m in ms if m.get("label") or m.get("query_id")]
            if labels:
                out[r["source_id"]] = list(dict.fromkeys(out.get(r["source_id"], []) + labels))
    return out


def _cap(text: str, n: int) -> str:
    return text if len(text) <= n else text[:n] + "\n…[truncated]"


def has_basis(row: Mapping[str, Any]) -> bool:
    """A question can be asked: the row has an abstract or an in-band excerpt."""
    return bool(row["metadata"].get("abstract")) or row.get("inband_text_sha256") is not None


def answer_of(ev: Mapping[str, Any], q: gate.Question) -> Row:
    """The ``QUESTION`` an evidence row gives (A.2)."""
    return {"question": q.name, "status": ev["status"], "error_class": ev["error_class"],
            "verdict": ev["verdict"], "confidence": ev["confidence"], "evidence_id": ev["evidence_id"],
            "config_digest": q.config_digest}


@dataclass
class _Pair:
    row: Row
    prompt: QuestionPrompt
    source_id: str
    license_class: str
    prev: Mapping[str, Any] | None
    user: str = ""
    basis: str = BASIS_FIELDS
    fresh: bool = False  # prev is of another basis or title variant: a first attempt that supersedes it


class LlmAsker:
    """The asker of :mod:`stack.gate`'s protocol (``name``, ``origin``, ``ask``) over the LLM
    client (module docstring)."""

    origin = gate.ASKED_ORIGIN

    def __init__(self, prompts: Sequence[QuestionPrompt], *, budget_usd: float | None,
                 max_rows: int | None = None, workers: int = 1, batch: BatchOptions | None = None,
                 titles: Sequence[QuestionPrompt] = ()) -> None:
        self.prompts = {p.question.name: p for p in prompts}
        self.titles = {t.question.name: t for t in titles}  # D527: title_basis variants; {} = off
        self.asks_without_basis = bool(self.titles)
        self.budget_usd = budget_usd
        self.max_rows = max_rows
        self.workers = workers
        self.batch = batch  # batch mode's options; None = sync
        self.name = asker_name(prompts, titles)

    def _client(self, ctx: StageContext) -> LlmClient:
        hook = annotate_stage.TRANSPORT_HOOK
        return open_client(annotate_stage.stack_config(), run_id=ctx.run_id, stage=ctx.stage,
                           store_root=ctx.store_root, budget_usd=self.budget_usd,
                           transport=hook() if hook is not None else None,
                           sleep=annotate_stage.SLEEP or time.sleep)

    def can_ask(self, row: Mapping[str, Any]) -> bool:
        """Whether :meth:`ask` would send a question for ``row`` (a gate-view row) rather than answer
        ``skipped[no_basis]`` without a call: an abstract or excerpt, or a title under ``title_basis``
        (:func:`stack.gate.can_ask`, D724)."""
        return has_basis(row) or (bool(self.titles) and has_title(row))

    def ask(self, rows: list[Row], questions: list[gate.Question], ctx: StageContext) -> gate.AskResult:
        now = annotate_stage.CLOCK() if annotate_stage.CLOCK is not None else dt.datetime.now(dt.UTC)
        counts = ctx.counts
        unknown = [q.name for q in questions if q.name not in self.prompts]
        if unknown:
            raise gate.GateError(f"triage asker {self.name}: questions {unknown} were not resolved")
        prior = ctx.view(EVIDENCE) if EVIDENCE in ctx.prior else None
        accepted = gate.accepted_answers(ctx.plan_stage, questions)  # D758: an accepted answer is final
        latest = _latest(prior, rows, questions, accepted, self)
        res = gate.AskResult()

        def bump(key: str) -> None:
            counts[key] = counts.get(key, 0) + 1

        queue: list[_Pair] = []
        for r in rows:  # the stage's order (module docstring): question scope first, then doc_id
            carried = gate.carried_answers(r, questions, accepted)
            for q in questions:
                if gate.is_final(carried.get(q.name), no_basis_final=not self.asks_without_basis):
                    continue  # not requested of the asker (stack.gate's rows contract)
                prompt, basis = self.prompts[q.name], BASIS_FIELDS
                if not has_basis(r):
                    if q.name not in self.titles or not has_title(r):
                        res.answers[(r["doc_id"], q.name)] = gate.question_row(q, "skipped", "no_basis")
                        bump("ask.no_basis")
                        continue
                    prompt, basis = self.titles[q.name], BASIS_TITLE  # D527
                b = basis_of(r, "fields")
                prev = latest.get(work_digest(b.source_id, r["metadata"]["fields_digest"],
                                              q.config_digest, ""))
                fresh = prev is not None and ((prev.get("basis") or BASIS_FIELDS) != basis or (
                    basis == BASIS_TITLE and prev.get("prompt_name") not in (None, prompt.name)))
                if not fresh and prior_decision(prev, now) == "carry":
                    assert prev is not None
                    res.answers[(r["doc_id"], q.name)] = answer_of(prev, q)
                    bump("ask.carried_evidence")
                    continue
                if basis == BASIS_TITLE:
                    bump("ask.title_basis")
                queue.append(_Pair(r, prompt, b.source_id, b.license_class or "restricted", prev,
                                   basis=basis, fresh=fresh))

        # Every excerpt is read before the first call: a missing blob fails the run unspent. The
        # reads run on a small pool (EXCERPT_READERS), each keeping only the start a message uses.
        digests = {p.row["doc_id"]: p.row.get("inband_text_sha256") for p in queue if p.basis == BASIS_FIELDS}
        records = {p.row["doc_id"]: topic_record_sha256(p.row) for p in queue if p.basis == BASIS_TITLE}
        with ThreadPoolExecutor(max_workers=EXCERPT_READERS, thread_name_prefix="triage-excerpt") as readers:
            excerpts = dict(zip(digests, readers.map(functools.partial(_excerpt, ctx), digests.values())))
            subj = dict(zip(records, readers.map(functools.partial(_subjects, ctx), records.values())))
        labels = match_labels(ctx, {m["source_id"] for p in queue if p.basis == BASIS_TITLE
                                    for m in p.row["identity"]["members"] or [] if m["is_live"]}) if records else {}
        for p in queue:
            if p.basis == BASIS_TITLE:
                live = [m["source_id"] for m in p.row["identity"]["members"] or [] if m["is_live"]]
                found = list(dict.fromkeys(x for s in live for x in labels.get(s, [])))
                p.user = title_record_text(p.prompt.name, p.row, subj[p.row["doc_id"]], found)
            else:
                p.user = record_text(p.prompt.name, p.row, excerpts[p.row["doc_id"]])

        client: LlmClient | None = None
        scope = BudgetScope()
        halted = False
        started: set[str] = set()
        workers = self.workers
        progress = annotate_stage.AskProgress(len(queue), workers, "triage")
        if self.batch is not None and queue:  # batch mode: collect passes + batches; the loop below replays
            client = bc = self._client(ctx)
            docs = list(dict.fromkeys(p.row["doc_id"] for p in queue))
            keep = set(docs if self.max_rows is None else docs[:self.max_rows])
            admitted = [p for p in queue if p.row["doc_id"] in keep]
            batchmode.run_batches(bc, lambda: (functools.partial(_complete, bc, p) for p in admitted),
                                  options=self.batch, label="triage", heartbeat=ctx.heartbeat,
                                  sleep=annotate_stage.SLEEP or time.sleep, counts=counts)
        commons: dict[int, dict[str, Any]] = {}  # queue index -> the evidence row's common fields
        outcomes: dict[int, LlmResult | LlmError | None] = {}
        pending = iter(enumerate(queue))
        active: dict[Future[LlmResult | None], int] = {}
        def left(p: _Pair, why: str) -> None:
            q_ = p.prompt.question
            res.answers[(p.row["doc_id"], q_.name)] = gate.question_row(q_, "not_attempted", why)
            bump("ask.not_attempted." + why)

        with DrainPool(max_workers=workers, thread_name_prefix="triage") as pool:
            progress.workers = pool.workers  # what can be in flight here (D786)
            more = bool(queue)
            while more or active:
                while more and len(active) < pool.workers:  # admission in queue order; the pool may be capped (D786)
                    nxt = next(pending, None)
                    if nxt is None:
                        more = False
                        break
                    n, p = nxt
                    q, d = p.prompt.question, p.row["doc_id"]
                    if halted or scope.tripped:
                        left(p, "halted" if halted else "budget")
                        continue
                    if ctx.drain.stopping():  # D570
                        left(p, "stopped")
                        continue
                    if d not in started and self.max_rows is not None and len(started) >= self.max_rows:
                        bump("ask.limit")  # left out: the gate makes it not_attempted[limit], scope incomplete
                        continue
                    started.add(d)
                    if client is None:
                        client = self._client(ctx)
                    base = None if p.fresh else p.prev  # D527: another basis's evidence is no earlier attempt
                    commons[n] = {
                        "doc_id": d, "source_id": p.source_id, "basis_digest": p.row["metadata"]["fields_digest"],
                        "config_digest": q.config_digest, "run_id": ctx.run_id, "now": now,
                        "attempts": base["attempts"] + 1 if base is not None else 1,
                        "first_attempt_at": base["first_attempt_at"] if base is not None else now,
                        "supersedes": p.prev["evidence_id"] if p.prev is not None else None}
                    active[pool.submit(scope.call, functools.partial(_complete, client, p))] = n
                progress.active, progress.workers = len(active), pool.workers
                if not active:
                    break
                if ctx.drain.expired():  # D571: the pairs still in flight are abandoned, not attempted
                    for fut, n in active.items():
                        fut.cancel()
                        left(queue[n], "stopped")
                    for _n, p in pending:  # not admitted yet
                        left(p, "halted" if halted else "budget" if scope.tripped else "stopped")
                    counts["ask.drain.abandoned"] = len(active)
                    active.clear()
                    pool.abandoned = True
                    break
                done, _ = wait(active, timeout=ctx.drain.remaining(0.5), return_when=FIRST_COMPLETED)
                for fut in done:
                    n = active.pop(fut)
                    try:
                        out: LlmResult | LlmError | None = fut.result()
                    except BatchUnanswered:  # batch mode: no answer this run; not a failure, not a stop
                        q_ = queue[n].prompt.question
                        res.answers[(queue[n].row["doc_id"], q_.name)] = gate.question_row(
                            q_, "not_attempted", "unanswered")
                        bump("ask.not_attempted.unanswered")
                        continue
                    except LlmError as exc:  # anything else is a code bug and fails the run
                        # kept until the run's evidence is assembled: without its traceback, whose frames hold
                        # the rendered request (about 70 KB a failed pair, D780)
                        exc.__traceback__ = None
                        out = exc
                        halted = halted or isinstance(exc, FatalLlmError)
                        progress.failed += 1
                        progress.spent += float(getattr(exc, "cost_usd", 0.0) or 0.0)
                    else:
                        progress.spent += out.cost_usd if out is not None else 0.0
                    outcomes[n] = out
                    ctx.heartbeat()
                progress.active = len(active)
                progress.show(len(outcomes), progress.failed, progress.spent)
        if outcomes:
            progress.show(len(outcomes), progress.failed, progress.spent, force=True, final=True)

        new: list[Row] = []
        for n in sorted(outcomes):  # deterministic assembly, whatever order the calls finished in
            p, out = queue[n], outcomes[n]
            q, d = p.prompt.question, p.row["doc_id"]
            common = commons[n]
            if annotate_stage.auth_stop(out):  # the key's stop, not the pair's: no attempt charged (D430)
                res.answers[(d, q.name)] = gate.question_row(q, "not_attempted", "halted")
                bump("ask.not_attempted.halted")
                continue
            if isinstance(out, LlmError):
                ev = evidence_row(EVIDENCE_SPEC, status=out.status, error_class=out.error_class,
                                  error=str(out)[:2000], payload=None, llm=None, origin=self.origin,
                                  cost_usd=getattr(out, "cost_usd", None), **common)
                ev["question"] = q.name
                ev["basis"] = p.basis
                bump(f"ask.failed.{out.error_class}")
            elif out is None:  # the budget scope tripped on this pair: never sent
                res.answers[(d, q.name)] = gate.question_row(q, "not_attempted", "budget")
                bump("ask.not_attempted.budget")
                continue
            else:
                parsed = out.parsed
                ev = evidence_row(EVIDENCE_SPEC, status="ok", llm=out, origin=self.origin, payload={
                    "question": q.name, "verdict": str(parsed["verdict"]),
                    "confidence": float(parsed["confidence"]), "reasoning": str(parsed["reasoning"])},
                    **common)
                ev["basis"] = p.basis  # D527: outside the payload (result_digest unchanged)
                bump("ask.asked")
                if out.is_cache_hit:
                    bump("ask.cache_hits")
            new.append(ev)
            res.answers[(d, q.name)] = answer_of(ev, q)
        if workers > 1:
            counts["ask.workers"] = workers
        if queue and pool.workers < workers:  # D786: capped below the line's ask; the threads it started
            counts["ask.workers.started"] = pool.started
        batchmode.replay_counts(client, counts)
        # the cumulative evidence as Arrow, in the order a view reads back (D780: the prior is never Python rows)
        new_tbl = pa.Table.from_pylist(new, schema=family(EVIDENCE).schema)
        if prior is not None:
            new_tbl = pa.concat_tables([prior, new_tbl.cast(prior.schema)])
        res.evidence = new_tbl.sort_by("evidence_id")
        return res


def _latest(prior: pa.Table | None, rows: Sequence[Row], questions: Sequence[gate.Question],
            accepted: Mapping[str, Sequence[str]], asker: LlmAsker) -> dict[str, Mapping[str, Any]]:
    """:func:`stack.annotate.evidence.by_work` over the prior evidence rows of the work keys :meth:`LlmAsker.ask` may
    look up (the pairs of ``rows`` without a final carried answer that have a basis), read from the prior view as
    Python rows only for those keys (D780: never the whole cumulative evidence file)."""
    if prior is None or prior.num_rows == 0:
        return {}
    keys: set[str] = set()
    for r in rows:
        carried = gate.carried_answers(r, questions, accepted)
        for q in questions:
            if gate.is_final(carried.get(q.name), no_basis_final=not asker.asks_without_basis):
                continue
            if not has_basis(r) and (q.name not in asker.titles or not has_title(r)):
                continue
            keys.add(work_digest(basis_of(r, "fields").source_id, r["metadata"]["fields_digest"], q.config_digest, ""))
    if not keys:
        return {}
    sub = prior.filter(pc.is_in(prior["work_digest"], value_set=pa.array(sorted(keys), pa.string())))
    return by_work(sub.to_pylist())


def _complete(client: LlmClient, p: _Pair) -> LlmResult:
    qp = p.prompt
    return client.complete(role=qp.role, model=qp.model, template=qp.template, refs={}, user=p.user,
                           schema=SCHEMA, license_class=cast(LicenseClass, p.license_class),
                           prompt_name=qp.name, basis_digest=p.row["metadata"]["fields_digest"])


def _subjects(ctx: StageContext, digest: str | None) -> dict[str, Any]:
    """:func:`subjects` of the OpenAlex record blob ``digest`` (``{}`` for ``None`` or a record that is not
    JSON); a missing blob fails the run before any call, as a missing excerpt does."""
    if digest is None:
        return {}
    import json

    from stack.store.blobs import BlobNotFound

    try:
        raw = ctx.blobs.get(digest)
    except BlobNotFound as exc:
        raise StageError(f"triage: provider_record blob {digest} is missing from the store") from exc
    try:
        return subjects(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return {}


def _excerpt(ctx: StageContext, digest: str | None) -> str | None:
    """The excerpt text of ``digest``, cut to ``MAX_EXCERPT_CHARS + 1`` characters (all that
    :func:`document_text` reads: its cap and its truncation mark come out the same)."""
    if digest is None:
        return None
    from stack.store.blobs import BlobNotFound

    try:
        return ctx.blobs.get(digest).decode("utf-8")[:MAX_EXCERPT_CHARS + 1]
    except BlobNotFound as exc:
        raise StageError(f"triage: in-band text blob {digest} is missing from the store") from exc


def llm_asker(plan: Plan, stage_line: StageLine) -> LlmAsker | None:
    """The run's LLM asker: ``None`` when the gate has no questions; else every question's pin
    resolved (a bad pin raises :class:`stack.gate.GateError` here, before any call), with the
    stage line's ``budget.max_usd`` (none: ``llm.yaml`` ``budget.per_run_usd``),
    ``options.max_rows`` (none: no cap), ``options.workers`` (default :data:`stack.annotate.stage.DEFAULT_WORKERS`) and ``options.llm_mode``
    (default ``sync``) with the ``batch_*`` options (:mod:`stack.annotate.batchmode`)."""
    qs = gate.questions(plan)
    if not qs:
        return None
    llm = annotate_stage.stack_config().llm
    budget = stage_line.budget.max_usd if stage_line.budget is not None else None
    raw = stage_line.options.get("max_rows")
    if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int) or raw < 0):
        raise gate.GateError(f"triage: options.max_rows must be a non-negative integer, got {raw!r}")
    workers = stage_line.options.get("workers", annotate_stage.DEFAULT_WORKERS)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= annotate_stage.MAX_WORKERS:
        raise gate.GateError(f"triage: options.workers must be an integer between 1 and "
                             f"{annotate_stage.MAX_WORKERS}, got {workers!r}")
    batch = batchmode.llm_mode(stage_line.options, gate.GateError, "triage")
    title_basis = stage_line.options.get("title_basis", False)
    if not isinstance(title_basis, bool):
        raise gate.GateError(f"triage: options.title_basis must be true or false, got {title_basis!r}")
    prompts = [resolve(q, llm) for q in qs]
    titles = [resolve_title(p, llm) for p in prompts] if title_basis else []
    return LlmAsker(prompts, budget_usd=budget, max_rows=raw, workers=workers, batch=batch, titles=titles)


__all__ = ["BASIS_FIELDS", "BASIS_TITLE", "EVIDENCE_SPEC", "MAX_ABSTRACT_CHARS", "MAX_EXCERPT_CHARS", "PIN_KEYS",
           "ROLE", "SCHEMA", "OPTIONS", "SPECS", "UNDIGESTED_OPTIONS", "VERDICTS", "Spec",
           "LlmAsker", "QuestionPrompt", "answer_of", "asker_name", "document_text", "document_text_v2", "has_basis",
           "has_title", "llm_asker", "match_labels", "provenance_lines", "record_text", "resolve", "resolve_title",
           "subjects", "title_record_text", "title_text", "title_text_v2", "topic_record_sha256"]
