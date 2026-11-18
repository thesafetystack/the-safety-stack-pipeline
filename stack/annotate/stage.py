"""The generic ``annotate.<wf>`` driver (§3.1): substages ``ask`` then ``select`` (D32;
``select`` is pure and always runs). A workflow module plugs in a :class:`Workflow`::

    # workflows/annotate/<wf>/main.py
    from stack.annotate import (OPTIONS, PURE_SUBSTAGES, SUBSTAGES, Answer, AskContext, AskItem, PromptPin,
                                Workflow, run_annotate)
    # OPTIONS (D434): the option keys the stage line may carry, checked by the runner; a workflow
    # reading more declares OPTIONS = stack.annotate.OPTIONS | frozenset({...}), as screen does.

    def ask(item: AskItem, actx: AskContext) -> Answer:          # one call per document
        res = actx.client.complete(role=..., template=..., refs=..., user=item.text, schema=...,
                                   license_class=item.license_class, prompt_name=actx.pin.name,
                                   basis_digest=item.basis_digest)
        return Answer(payload={...}, llm=res)                    # status 'ok' or 'empty'

    def prompt(pin: Mapping[str, Any], llm: LlmConfig) -> PromptPin: ...   # no I/O

    WORKFLOW = Workflow("screen", ask=ask, prompt=prompt)
    def run(ctx): return run_annotate(ctx, WORKFLOW)

**One run** (:func:`run_annotate`):

1. the reads (:mod:`stack.annotate.inputs`, D235): the inputs other than ``corpus`` are exactly
   the joined set (the registry ``reads`` of the workflow, then the stage line's ``reads:``),
   each joined into the corpus rows' ``annotations`` with mapping M; then scope
   (:mod:`stack.annotate.scope`) over the joined rows;
2. the workflow's config: the plan pin ``prompts.annotate.<wf>`` → ``workflow.prompt(pin,
   llm)`` → :class:`PromptPin`; ``config_digest`` = :func:`config_digest` (also the run's and
   both files' header ``config_digest``); the run record's ``prompt_pin`` = :func:`pin_record`
   (what that digest hashes, plus the produced records and the resolved LLM role's parameters,
   ``role_params``, neither hashed; R1-1: X6 reads it; the release exports the parameters from it);
   **accepted configs** (D758, :mod:`stack.plan` "Accepted configs"): the line's ``accepted:`` list names
   earlier configs whose decided answers count (preference order). Before any call the line's ``target`` must be
   ``config_digest``, and each accepted config must be one this stage recorded (a run record whose
   ``prompt_pin`` hashes to it) with the same ``workflow``, ``registry_version`` and ``produced`` as the current
   pin and the ``name`` :func:`config_name` gives it; else the run is refused (:class:`StageError`), so an
   accepted row never lacks a record or carries another payload version. The run record's ``selection`` is the
   line's (:meth:`stack.plan.StageLine.selection`: target, accepted list, selection digest), plus
   ``same_basis: true`` (D807, below; outside the digest, so a release's ``stages`` do not move);
3. ``ask``: per row with a basis, the work key's selected prior evidence decides
   (:func:`stack.annotate.evidence.prior_decision`): carry (no call) or ask; a row without
   evidence under the current key is carried when it has an accepted row
   (:func:`stack.annotate.evidence.accepted_row`: counts ``carried`` and ``carried.accepted``); a row whose current
   key has no decided answer (none, or a failure, as in ``select``) is carried when another
   supplying member's decided answer on the same basis and config stands for it (D807,
   :func:`stack.annotate.evidence.same_basis_row`: the pointer moved to a member with the same text whose licence
   class is no stricter; counts ``carried`` and ``carried.same_basis``), else asked; within
   ``options.max_rows`` (rest ``not_attempted[limit]``); a :class:`~stack.llm.BudgetExceeded`
   (the run's budget) trips :class:`~stack.llm.BudgetScope`: that row and every later one
   ``not_attempted[budget]``, nothing sent; an ``ask`` returning None (the workflow's own allowance,
   screen's tier 2, D256) is ``not_attempted[budget]`` without tripping the scope, counted
   ``not_attempted.deferred`` (build reason ``tier2_allowance``, D706) and not ``not_attempted.budget``;
   a :class:`~stack.llm.FatalLlmError` halts the run
   (every later row ``not_attempted[halted]``) and is recorded on its row, except an auth/credit
   stop (:func:`auth_stop`), whose rows are ``not_attempted[halted]`` too, no attempt charged; any other
   :class:`~stack.llm.LlmError` is an evidence row with the error's ``(status, error_class)``
   (a transient network/5xx/429 failure the client gave up on, :class:`~stack.llm.AttemptsExhausted`,
   is ``failed_transient`` with ``retry_after``; the run goes on); any other exception fails the
   run (``code_bug``). ``options.workers`` (default :data:`DEFAULT_WORKERS`, ≤ :data:`MAX_WORKERS`) asks that many documents at once
   from a thread pool; admission (halt, budget, limit) is decided in ``doc_id`` order as rows
   are submitted, documents in flight when the run halts or the budget trips finish and are
   kept, and the evidence rows are assembled in ``doc_id`` order afterwards. A drain (D570, D571;
   ``ctx.drain``) admits no further row: the rest is ``not_attempted[stopped]`` (count
   ``not_attempted.stopped``, build reason ``stopped``); calls in flight get the drain's bounded
   wait, then are abandoned, their rows ``not_attempted[stopped]`` too (their late answers are
   dropped, the next run asks them; a cached response serves it free). An
   :class:`Answer` with ``followups`` (a second tier) writes one evidence row per tier, each
   ``supersedes`` the one before; ``select`` keeps the newest decided one;
4. ``select`` (:mod:`stack.annotate.select`; with the applied ``from``, so an accepted row is selected
   when the current key has no decided row, then a same-basis row of another supplier, D807) over the scope, all evidence and the ``<wf>``
   rows of the pinned ``overrides`` input (``--in data/overrides/<series>/``; required when the
   store has an ``overrides`` chain, :func:`stack.overrides.input_rows`), revokes applied;
5. emit ``<wf>`` (full) and, for ``evidence: true`` workflows, ``<wf>_evidence`` (full: every
   prior evidence row then this run's). ``scope_complete`` is false when any row stayed
   ``not_attempted``.

**Batch mode** (stage line ``options.llm_mode: batch``; :mod:`stack.annotate.batchmode`,
:mod:`stack.llm.batch`): before the loop, the admitted documents (``max_rows`` in ``doc_id``
order) run ``workflow.ask`` in collect passes whose requests go out as OpenRouter batches at half
price (a pass's answers may admit further calls: screen's tier 2); the loop then runs unchanged as
the replay pass, served from the batch outcomes. A document the batches left without an answer
(``batch_wait_minutes`` time cap, a failed/expired batch, a submit error) is
``not_attempted[unanswered]`` (count ``not_attempted.unanswered``; the next run asks it again);
counts ``batch.*`` (:class:`stack.llm.batch.BatchReport`, plus ``batch.sync_fallback``).

**Annotation records** (registry ``produced_by``; :func:`stack.schemas.payloads.registry.records_of`).
A workflow declares ``records(plan_pin)`` = the records its pin produces (default none; each must
be a registered record of this workflow). An :class:`Answer` (and each followup) then carries
``records`` = record name → payload, which :func:`stack.annotate.evidence.evidence_row` writes
into the evidence row's record struct columns. ``select`` runs once per produced record over the
same scope and evidence, with the record's own overrides (target ``<record>``)
(:mod:`stack.annotate.select`), and the run emits one ``<record>`` full file per produced record
beside ``<wf>`` and ``<wf>_evidence``; a decided evidence row missing a produced record's payload
fails the run (:class:`~stack.stage_runner.StageError`). Counts ``rows.<record>``, ``overridden.<record>``.

**Seams** (test-only, set like ``stack.gate.ASKER_HOOK``): :data:`TRANSPORT_HOOK` returns the
LLM transport (default: ``$STACK_LLM_TRANSPORT`` if set, else the production transport; tests
return ``tests.fakes.llm.FakeLlm``); :data:`CLOCK` returns the run's ``now`` (read once per
run); :data:`SLEEP` replaces the client's backoff sleep. The ledger is the run's own, in memory
(:func:`stack.llm.ledger.run_ledger`).
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import sys
import threading
import time
from collections.abc import Callable, Mapping
from concurrent.futures import FIRST_COMPLETED, Future, wait
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pyarrow as pa

from stack.annotate.basis import basis_of, licenses_of, work_digest
from stack.annotate import batchmode
from stack.annotate.batchmode import llm_mode, replay_counts, run_batches
from stack.annotate.evidence import OK_LIKE, accepted_row, by_basis, by_work, evidence_row, prior_decision, same_basis_row
from stack.annotate.inputs import join_reads
from stack.annotate.scope import Limit, in_scope
from stack.annotate.select import RecordMissing, select
from stack.checks.digests import H
from stack.llm import DEFAULT_WORKERS as LLM_DEFAULT_WORKERS
from stack.llm import MAX_WORKERS as LLM_MAX_WORKERS
from stack.llm import BatchUnanswered, BudgetScope, FatalLlmError, LlmError, open_client
from stack.overrides import FAMILY as OVERRIDES
from stack.overrides import OverrideError, input_rows
from stack.overrides import active as active_overrides
from stack.schemas.families import family
from stack.schemas.payloads.registry import REGISTRY, PayloadSpec, joined_reads, payload, records_of
from stack.stage_runner import DrainPool, StageContext, StageError, StageResult

if TYPE_CHECKING:
    from stack.config import LlmConfig, StackConfig
    from stack.llm import LlmClient, Transport

SUBSTAGES: tuple[str, ...] = ("ask", "select")
PURE_SUBSTAGES: tuple[str, ...] = ("select",)
#: The ``options`` keys :func:`run_annotate` reads (``max_rows``, ``workers``, batch mode); an
#: ``annotate.<wf>`` module declares these plus its own as ``OPTIONS`` (D434).
OPTIONS: frozenset[str] = frozenset({"max_rows", "workers"}) | batchmode.OPTIONS

#: ``() -> Transport``; None = the production transport (``stack.llm.make_default_transport``).
TRANSPORT_HOOK: Callable[[], Transport] | None = None
#: ``() -> datetime``; None = ``datetime.now(UTC)``.
CLOCK: Callable[[], dt.datetime] | None = None
#: The client's backoff sleep; None = ``time.sleep``.
SLEEP: Callable[[float], None] | None = None
#: ``options.workers`` bounds (v3's ``STACK_SCREEN_WORKERS``), also triage's: :data:`stack.llm.MAX_WORKERS` (1024 since
#: D716, 256 since D692, was 32; the calls are network-bound threads over one client) and the count when a line sets
#: none, :data:`stack.llm.DEFAULT_WORKERS` (64 since D692, was 1). Plan batch-2026-10a sets 256 on triage, screen and
#: affiliation and 1024 on technique_staged and model_roles.
MAX_WORKERS = LLM_MAX_WORKERS
DEFAULT_WORKERS = LLM_DEFAULT_WORKERS


@dataclass(frozen=True)
class PromptPin:
    """What a workflow sends, resolved from the plan pin without I/O: the prompt name it writes
    as ``prompt_name``, every template digest it may send (``template_digest``; two tiers →
    two), and the models it may call. X6 recomputes it from the plan."""

    name: str | None = None
    template_digests: tuple[str, ...] = ()
    models: tuple[str, ...] = ()


@dataclass(frozen=True)
class AskItem:
    """One document to ask about. :attr:`text` (``basis_kind = 'text'`` or ``'chained'``) is the
    current text blob, read on first access (a missing blob fails the run); ``fields``
    (``'fields'``) the ``metadata`` block; ``row`` is the whole ``corpus`` row, its
    ``annotations`` holding the joined reads (D235). ``license_class`` is the supplying
    member's (NULL → ``'restricted'``), for the egress gate. ``attempt`` counts invocations of
    this work key (1 = first)."""

    doc_id: str
    source_id: str
    basis_kind: str
    basis_digest: str
    fields: Mapping[str, Any] | None
    row: Mapping[str, Any]
    license_class: str
    attempt: int
    load_text: Callable[[], str] | None = field(default=None, repr=False)

    @functools.cached_property
    def text(self) -> str | None:
        return self.load_text() if self.load_text is not None else None


@dataclass(frozen=True)
class Answer:
    """A workflow's answer for one document: ``payload`` (the registry type's top-level fields;
    item grain: ``{"items": [...]}``, each item with its ``item_key``), ``status`` ``ok`` or
    ``empty``, and ``llm`` = the :class:`~stack.llm.LlmResult` (or a mapping of its EVI columns,
    when several calls make one answer); ``llm = None`` = computed without a model (origin
    ``computed``).

    ``followups``: later tiers of the same invocation (screen's adjudication), in order, each an
    :class:`Answer` (without followups of its own) or the :class:`~stack.llm.LlmError` the tier
    failed with. Each becomes its own evidence row that ``supersedes`` the previous one, with
    this answer's attempts; ``select`` keeps the newest decided row, so a failed tier leaves
    the earlier tier's result selected. ``counts`` are added to the run's counts. ``records``:
    record name → payload of each annotation record the workflow's pin produces (module
    docstring), written into this answer's evidence row."""

    payload: Mapping[str, Any]
    llm: Any | None = None
    status: str = "ok"
    followups: tuple[Answer | LlmError, ...] = ()
    counts: Mapping[str, int] = field(default_factory=dict)
    records: Mapping[str, Mapping[str, Any] | None] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in ("ok", "empty"):
            raise ValueError(f"Answer.status {self.status!r}: an answer is 'ok' or 'empty'; "
                             f"failures are raised (stack.llm.LlmError)")
        for f in self.followups:
            if isinstance(f, Answer) and f.followups:
                raise ValueError("a followup Answer has no followups of its own")
            if not isinstance(f, (Answer, LlmError)):
                raise TypeError(f"Answer.followups: {type(f).__name__} is neither Answer nor LlmError")


@dataclass
class AskContext:
    """What ``ask`` gets besides the item: the run's ids, the plan pin and its
    :class:`PromptPin`, the stage line's ``options``, and :attr:`client` (an
    :class:`~stack.llm.LlmClient` opened on first use with the stage's ``budget.max_usd``)."""

    run_id: str
    stage: str
    store_root: Any
    pin: PromptPin
    plan_pin: Mapping[str, Any]
    options: Mapping[str, Any]
    budget_usd: float | None
    _config: Callable[[], StackConfig]
    _client: LlmClient | None = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def config(self) -> StackConfig:
        return self._config()

    @property
    def client(self) -> LlmClient:
        with self._lock:  # one client per run, whichever worker asks first
            if self._client is None:
                transport = TRANSPORT_HOOK() if TRANSPORT_HOOK is not None else None
                self._client = open_client(self.config, run_id=self.run_id, stage=self.stage,
                                           store_root=self.store_root, budget_usd=self.budget_usd,
                                           transport=transport, sleep=SLEEP or time.sleep)
            return self._client

    @property
    def opened(self) -> bool:
        return self._client is not None


def _no_records(_pin: Mapping[str, Any]) -> tuple[str, ...]:
    return ()


@dataclass(frozen=True)
class Workflow:
    """A workflow plugged into the framework: its registry name, the per-document ``ask``, the
    prompt resolver (``None`` for a workflow without prompts) and ``records(plan_pin)``, the
    annotation records the pin produces (module docstring; default none)."""

    name: str
    ask: Callable[[AskItem, AskContext], Answer]
    prompt: Callable[[Mapping[str, Any], LlmConfig], PromptPin] | None = None
    records: Callable[[Mapping[str, Any]], tuple[str, ...]] = _no_records

    @property
    def spec(self) -> PayloadSpec:
        return payload(self.name)

    def produced(self, plan_pin: Mapping[str, Any]) -> tuple[str, ...]:
        """``records(plan_pin)``, checked: each a registered record of this workflow (registry
        order); anything else is refused (:class:`StageError`)."""
        got = tuple(self.records(plan_pin))
        mine = records_of(self.name)
        bad = [r for r in got if r not in mine]
        if bad:
            raise StageError(f"annotate.{self.name}: records {bad} are not registered records of "
                             f"{self.name} ({list(mine)})")
        return tuple(r for r in mine if r in got)


def config_digest(spec: PayloadSpec, plan_pin: Mapping[str, Any], pin: PromptPin) -> str:
    """A workflow's config digest (the work keys' ``config_digest``): ``H({workflow,
    registry_version, plan_pin, prompt_name, template_digests (sorted), models})``."""
    return H(_pin_fields(spec, plan_pin, pin))


def _pin_fields(spec: PayloadSpec, plan_pin: Mapping[str, Any], pin: PromptPin) -> dict[str, Any]:
    return {"workflow": spec.workflow, "registry_version": spec.version, "plan_pin": dict(plan_pin),
            "prompt_name": pin.name, "template_digests": sorted(pin.template_digests),
            "models": list(pin.models)}


#: The keys of :func:`pin_record` that :func:`config_digest` hashes (``produced`` and ``role_params`` are not).
PIN_DIGEST_KEYS: tuple[str, ...] = ("workflow", "registry_version", "plan_pin", "prompt_name",
                                    "template_digests", "models")


def pin_record(spec: PayloadSpec, plan_pin: Mapping[str, Any], pin: PromptPin,
               produced: tuple[str, ...], role_params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """The run record's ``prompt_pin`` (R1-1): what :func:`config_digest` hashes plus the records
    the pin produces, so ``H({k: rec[k] for k in PIN_DIGEST_KEYS})`` is the run's
    ``config_digest`` and X6 can judge the run's evidence against the pin it asked under; and, when
    given, ``role_params`` (:func:`role_params`, D758): the LLM role's parameters as resolved for the run,
    which the release exports under this config instead of today's ``llm.yaml``."""
    out = {**_pin_fields(spec, plan_pin, pin), "produced": list(produced)}
    if role_params is not None:
        out["role_params"] = dict(role_params)
    return out


#: The ``llm.yaml`` role a workflow asks under when its plan pin names none (``role``).
DEFAULT_ROLES: dict[str, str] = {"screen": "screen", "affiliation": "extract_main", "model_roles": "extract_main",
                                 "technique_staged": "extract_staged"}


def role_params(wf: str, plan_pin: Mapping[str, Any], llm: LlmConfig) -> dict[str, Any] | None:
    """The LLM role ``plan_pin`` names (``role``, else :data:`DEFAULT_ROLES`) with its parameters in ``llm``:
    ``{name, model, fallbacks, max_tokens, temperature, reasoning_effort}`` (``{name}`` alone when ``llm`` has no
    such role); ``None`` for a workflow without a role (computed)."""
    name = plan_pin.get("role") or DEFAULT_ROLES.get(wf)
    if name is None:
        return None
    role = llm.roles.get(name)
    if role is None:
        return {"name": name}
    return {"name": name, "model": role.model, "fallbacks": list(role.fallbacks), "max_tokens": role.max_tokens,
            "temperature": role.temperature, "reasoning_effort": role.reasoning_effort}


@functools.lru_cache(maxsize=1)
def _vendored_tags() -> dict[str, str]:
    """sha256 → tag of every vendored taxonomy file (``taxonomy/vendored.lock.yaml``)."""
    from stack import yamlio
    from stack.stage_runner import REPO_ROOT

    lock = yamlio.load((REPO_ROOT / "taxonomy" / "vendored.lock.yaml").read_text("utf-8")) or {}
    return {str(e["sha256"]): str(e["tag"]) for e in (lock.get("files") or {}).values()}


def config_name(pin: Mapping[str, Any]) -> str:
    """A recorded config's readable name (D758; an ``accepted:`` entry's ``name`` must be it): the pin's prompt
    name (else its plan pin's ``name``, else the workflow), then ``@<tag>`` of each vendored taxonomy file whose
    sha256 the pin's template digests carry: ``technique-staged-v5-a1.1@techniques-1.0.0``,
    ``model-roles-v3b@models-1.0.0``."""
    name = pin.get("prompt_name") or (pin.get("plan_pin") or {}).get("name") or pin.get("workflow") or "?"
    tags = _vendored_tags()
    return "@".join([str(name), *sorted({tags[d] for d in pin.get("template_digests") or [] if d in tags})])


def recorded_pins(store_root: Any, stage: str) -> dict[str, Mapping[str, Any]]:
    """Config digest → the ``prompt_pin`` a run record of ``stage`` recorded under it."""
    from stack.store.index import store_index

    pins: dict[str, Mapping[str, Any]] = {}
    for rec in store_index(store_root).records():
        p = rec.prompt_pin
        if rec.stage == stage and p and all(k in p for k in PIN_DIGEST_KEYS):
            pins.setdefault(H({k: p[k] for k in PIN_DIGEST_KEYS}), p)
    return pins


def check_accepted(store_root: Any, line: Any, cfg: str, current: Mapping[str, Any]) -> None:
    """The stage-start check of a line's ``target`` and ``accepted`` (module docstring, step 2); a refusal is a
    :class:`StageError` before any call."""
    stage = line.stage
    if line.target is not None and line.target != cfg:
        raise StageError(f"{stage}: the line's target {line.target[:12]}… is not the config its pin gives "
                         f"({cfg[:12]}…): the pin changed since the accepted list was written; update `target` "
                         f"(and decide whether the accepted configs still count) or drop the list")
    if not line.accepted:
        return
    pins = recorded_pins(store_root, stage)
    for e in line.accepted:
        where = f"{stage}: accepted {e.config[:12]}… ({e.name})"
        got = pins.get(e.config)
        if got is None:
            raise StageError(f"{where} is no config this stage recorded (no run record's prompt_pin hashes to it)")
        diff = [k for k in ("workflow", "registry_version", "produced") if _listed(got.get(k)) != _listed(current.get(k))]
        if diff:
            raise StageError(f"{where} has another {', '.join(diff)} than the current config "
                             f"({', '.join(f'{k} {got.get(k)!r} vs {current.get(k)!r}' for k in diff)}): its rows "
                             f"cannot stand for this config's")
        if e.name != config_name(got):
            raise StageError(f"{where}: name {e.name!r} is not the recorded config's {config_name(got)!r}")


def _listed(v: Any) -> Any:
    return list(v) if isinstance(v, (list, tuple)) else v


def resolve_pin(workflow: Workflow, plan_pin: Mapping[str, Any],
                config: Callable[[], StackConfig]) -> PromptPin:
    """``workflow.prompt(plan_pin, llm)``, or an empty pin for a workflow without prompts. A plan
    pin naming a ``template_digest`` the workflow does not send is refused (:class:`StageError`)."""
    pin = workflow.prompt(plan_pin, config().llm) if workflow.prompt is not None else PromptPin()
    want = plan_pin.get("template_digest")
    if want is not None and want not in pin.template_digests:
        raise StageError(f"annotate.{workflow.name}: plan pins template_digest {want} but the "
                         f"workflow sends {list(pin.template_digests)}")
    return pin


def pinned_records(plan: Any, producer: str) -> tuple[str, ...]:
    """The records ``annotate.<producer>`` writes under ``plan``'s pin
    (``prompts.annotate.<producer>``): the stage module's ``WORKFLOW.produced(pin)``. Loads the
    workflow module (no other I/O); a producer without records returns ``()`` without loading."""
    if not records_of(producer):
        return ()
    from stack.stage_runner import load_stage_module

    module = load_stage_module(f"annotate.{producer}")
    wf: Workflow = module.WORKFLOW
    return wf.produced(dict(plan.prompts.get(f"annotate.{producer}", {})))


def _now() -> dt.datetime:
    return CLOCK() if CLOCK is not None else dt.datetime.now(dt.UTC)


@functools.lru_cache(maxsize=4)
def _load_config(config_dir: str | None) -> StackConfig:
    from stack.config import load

    return load(Path(config_dir) if config_dir else None)


def stack_config() -> StackConfig:
    """The validated config registry (``stack.config.load``; ``$STACK_CONFIG_DIR`` honoured),
    loaded once per process and config directory (~1 s each: the taxonomy YAML)."""
    return _load_config(os.environ.get("STACK_CONFIG_DIR"))


def run_annotate(ctx: StageContext, workflow: Workflow) -> StageResult:
    """Run ``annotate.<workflow.name>`` (module docstring)."""
    spec = workflow.spec
    wf = spec.workflow
    if ctx.stage != f"annotate.{wf}":
        raise StageError(f"{ctx.stage}: workflow {wf!r} runs as stage annotate.{wf}")
    if "corpus" not in ctx.inputs:
        raise StageError(f"{ctx.stage} reads the build's fold.corpus file: pass --in <this build's corpus file> (data/corpus/<series>/<name>.full.parquet)")
    reads = joined_reads(wf, ctx.plan_stage.reads)
    extra = sorted(set(ctx.inputs) - {"corpus", OVERRIDES, *reads})
    if extra:
        raise StageError(f"{ctx.stage}: inputs other than corpus, overrides and the joined reads {list(reads)} "
                         f"are not read: {extra}")
    try:
        override_rows = input_rows(ctx)
    except OverrideError as exc:
        raise StageError(str(exc)) from None
    missing = [n for n in reads if n not in ctx.inputs]
    if missing:
        raise StageError(f"{ctx.stage}: reads {missing} (registry reads {list(spec.reads)}, line reads "
                         f"{list(ctx.plan_stage.reads)}) need --in <this build's file> of each (D235)")
    counts = ctx.counts
    now = _now()
    config = stack_config
    plan_pin = dict(ctx.plan.prompts.get(f"annotate.{wf}", {}))
    pin = resolve_pin(workflow, plan_pin, config)
    cfg = config_digest(spec, plan_pin, pin)
    produced = workflow.produced(plan_pin)
    ctx.config_digest = cfg
    ctx.prompt_pin = pin_record(spec, plan_pin, pin, produced, role_params(wf, plan_pin, config().llm))
    check_accepted(ctx.store_root, ctx.plan_stage, cfg, ctx.prompt_pin)
    accepted = ctx.plan_stage.accepted_configs()
    # D807: the run selects another supplier's same-basis answers; recorded beside (not in) the selection digest
    ctx.selection = ctx.plan_stage.selection(cfg) | {"same_basis": True}

    corpus = join_reads(ctx.view("corpus"), {n: ctx.view(n) for n in reads})
    docs = in_scope(corpus, ctx.filter, counts)
    ev_family = f"{wf}_evidence"
    prior_ev = ctx.view(ev_family).to_pylist() if ev_family in ctx.prior else []
    counts["prior_evidence"] = len(prior_ev)
    latest = by_work(prior_ev)
    same = by_basis(latest)
    line = ctx.plan_stage
    budget = line.budget.max_usd if line.budget is not None else None
    actx = AskContext(run_id=ctx.run_id, stage=ctx.stage, store_root=ctx.store_root, pin=pin,
                      plan_pin=plan_pin, options=dict(line.options), budget_usd=budget, _config=config)
    new_ev: list[dict[str, Any]] = []
    unreached: dict[str, tuple[str, str]] = {}

    with ctx.substage("ask") as sub:
        limit = Limit.from_options(line.options)
        workers = _workers(line.options)
        batch = llm_mode(line.options, StageError, ctx.stage)
        scope = BudgetScope()
        queue: list[tuple[dict[str, Any], Any, Mapping[str, Any] | None]] = []
        for d in docs:
            b = basis_of(d, spec.basis_kind, spec.reads)
            if b.digest is None:
                counts[f"skipped.{b.missing}"] = counts.get(f"skipped.{b.missing}", 0) + 1
                continue
            prev = latest.get(work_digest(b.source_id, b.digest, cfg, ""))
            if prev is None and accepted_row(latest, b.source_id, b.digest, accepted) is not None:
                counts["carried"] = counts.get("carried", 0) + 1  # D758: an accepted row stands for the current key
                counts["carried.accepted"] = counts.get("carried.accepted", 0) + 1
                continue
            if (prev is None or prev["status"] not in OK_LIKE) and same_basis_row(
                    same, d["doc_id"], b.source_id, b.digest, cfg, licenses_of(d)) is not None:
                counts["carried"] = counts.get("carried", 0) + 1  # D807: the supplier moved, the text did not
                counts["carried.same_basis"] = counts.get("carried.same_basis", 0) + 1
                continue
            if prior_decision(prev, now) == "carry":
                counts["carried"] = counts.get("carried", 0) + 1
                continue
            if not sub.active:
                unreached[d["doc_id"]] = ("not_attempted", "limit")
                continue
            queue.append((d, b, prev))

        halted: str | None = None
        outcomes: dict[str, tuple[dict[str, Any], Answer | LlmError | None]] = {}
        deferred: set[str] = set()  # ask returned None: the workflow deferred the row, not the budget scope

        def ask_one(item: AskItem) -> Answer | None:
            got = workflow.ask(item, actx)
            if got is None:
                deferred.add(item.doc_id)
            return got

        def build(d: dict[str, Any], b: Any, prev: Mapping[str, Any] | None) -> tuple[AskItem, dict[str, Any]]:
            attempt = prev["attempts"] + 1 if prev is not None else 1
            item = AskItem(doc_id=d["doc_id"], source_id=b.source_id, basis_kind=b.kind,
                           basis_digest=b.digest, fields=d["metadata"] if b.kind == "fields" else None,
                           row=d, license_class=b.license_class or "restricted", attempt=attempt,
                           load_text=_text_loader(ctx, d, b.kind))
            common = {"doc_id": d["doc_id"], "source_id": b.source_id, "basis_digest": b.digest,
                      "config_digest": cfg, "run_id": ctx.run_id, "now": now, "attempts": attempt,
                      "first_attempt_at": prev["first_attempt_at"] if prev is not None else now,
                      "supersedes": prev["evidence_id"] if prev is not None else None}
            return item, common

        if batch is not None and queue:  # batch mode: collect passes + batches; the loop below replays
            admitted = queue if limit.max_rows is None else queue[:limit.max_rows]
            run_batches(actx.client, lambda: (functools.partial(workflow.ask, build(*q)[0], actx) for q in admitted),
                        options=batch, label=wf, heartbeat=ctx.heartbeat, sleep=SLEEP or time.sleep, counts=counts)
        progress = AskProgress(len(queue), workers, wf)

        def admit(doc_id: str) -> str | None:
            if halted is not None:
                return "halted"
            if scope.tripped:
                return "budget"
            if ctx.drain.stopping():  # D570
                return "stopped"
            if not limit.take(doc_id):
                return "limit"
            return None

        pending = iter(queue)
        active: dict[Future[Answer | None], tuple[str, dict[str, Any]]] = {}
        with DrainPool(max_workers=workers, thread_name_prefix=f"annotate-{wf}") as pool:
            progress.workers = pool.workers  # what can be in flight here (D786)
            more = bool(queue)
            if more:
                progress.show(0, 0, 0.0, force=True)
            while more or active:
                while more and len(active) < pool.workers:  # the container may cap the pool (D786)
                    nxt = next(pending, None)
                    if nxt is None:
                        more = False
                        break
                    d, b, prev = nxt
                    why = admit(d["doc_id"])
                    if why is not None:
                        unreached[d["doc_id"]] = ("not_attempted", why)
                        continue
                    item, common = build(d, b, prev)
                    fut = pool.submit(scope.call, functools.partial(ask_one, item))
                    active[fut] = (d["doc_id"], common)
                progress.active, progress.workers = len(active), pool.workers
                if not active:
                    break
                if ctx.drain.expired():  # D571: the calls still in flight are abandoned, not attempted
                    for fut, (doc_id, _common) in active.items():
                        fut.cancel()
                        unreached[doc_id] = ("not_attempted", "stopped")
                    for d, _b, _prev in pending:  # not admitted yet: what admission would say now
                        unreached[d["doc_id"]] = ("not_attempted", admit(d["doc_id"]) or "stopped")
                    counts["drain.abandoned"] = len(active)
                    active.clear()
                    pool.abandoned = True
                    break
                done, _ = wait(active, timeout=ctx.drain.remaining(0.5), return_when=FIRST_COMPLETED)
                for fut in done:
                    doc_id, common = active.pop(fut)
                    try:
                        outcome: Answer | LlmError | None = fut.result()
                    except BatchUnanswered:  # batch mode: no answer this run; not a failure, not a stop
                        unreached[doc_id] = ("not_attempted", "unanswered")
                        continue
                    except LlmError as exc:  # any other exception fails the run (code_bug)
                        outcome = exc
                        if isinstance(exc, FatalLlmError) and halted is None:
                            halted = exc.error_class
                    outcomes[doc_id] = (common, outcome)
                    progress.add(outcome)
                progress.active = len(active)
                progress.show(len(outcomes), progress.failed, progress.spent)
                # Each round: the run record's counts for `stack status`, and a self-cancelled run
                # (D354) stops here rather than asking its whole queue to discard it at publish (L5-7).
                ctx.heartbeat()
        if queue:
            progress.show(len(outcomes), progress.failed, progress.spent, force=True, final=True)

        for doc_id in sorted(outcomes):  # deterministic assembly, whatever order calls finished in
            common, outcome = outcomes[doc_id]
            if outcome is None:  # the budget scope tripped on this row, or the workflow deferred it: never sent
                unreached[doc_id] = ("not_attempted", "budget")
                continue
            if auth_stop(outcome):  # the key's stop, not the document's: no attempt charged (D430)
                unreached[doc_id] = ("not_attempted", "halted")
                continue
            if isinstance(outcome, LlmError):
                new_ev.append(_failure_row(spec, outcome, common))
                counts[f"failed.{outcome.error_class}"] = counts.get(f"failed.{outcome.error_class}", 0) + 1
                continue
            row = evidence_row(spec, status=outcome.status, payload=outcome.payload, llm=outcome.llm,
                               origin="ai_check" if outcome.llm is not None else "computed",
                               records=_records(outcome, produced), **common)
            new_ev.append(row)
            counts["asked"] = counts.get("asked", 0) + 1
            if outcome.llm is not None and row["is_cache_hit"]:
                counts["cache_hits"] = counts.get("cache_hits", 0) + 1
            for k, v in outcome.counts.items():
                counts[k] = counts.get(k, 0) + v
            for f in outcome.followups:  # later tiers: each its own row, superseding the one before
                tier = {**common, "supersedes": new_ev[-1]["evidence_id"]}
                if isinstance(f, LlmError):
                    new_ev.append(_failure_row(spec, f, tier))
                else:
                    new_ev.append(evidence_row(spec, status=f.status, payload=f.payload, llm=f.llm,
                                               origin="ai_check" if f.llm is not None else "computed",
                                               records=_records(f, produced), **tier))
        sub.scope_complete = not unreached
        if scope.tripped:
            counts["not_attempted.budget"] = sum(v == ("not_attempted", "budget") and k not in deferred
                                                 for k, v in unreached.items())
        # rows not_attempted[budget] by the workflow's own allowance (screen's tier 2, D706), not by the scope
        if n_deferred := sum(v == ("not_attempted", "budget") and k in deferred for k, v in unreached.items()):
            counts["not_attempted.deferred"] = n_deferred
        if stopped := sum(v == ("not_attempted", "stopped") for v in unreached.values()):
            counts["not_attempted.stopped"] = stopped
        # options.max_rows (or an inactive ask substage): rows left for a later run, reason `limit` (D709)
        if limited := sum(v == ("not_attempted", "limit") for v in unreached.values()):
            counts["not_attempted.limit"] = limited
        if batch is not None:
            counts["not_attempted.unanswered"] = sum(v == ("not_attempted", "unanswered") for v in unreached.values())
            replay_counts(actx._client, counts)
        if halted is not None:
            counts["halted"] = 1
        if workers > 1:
            counts["workers"] = workers
        if queue and pool.workers < workers:  # D786: capped below the line's ask; the threads it started
            counts["workers.started"] = pool.started

    evidence = prior_ev + new_ev
    with ctx.substage("select"):
        entries, warned = active_overrides(override_rows, wf)
        rows = select(spec, docs, evidence, entries, config_digest=cfg, run_id=ctx.run_id, now=now,
                      unreached=unreached, accepted=accepted, same_basis=True)
        if warned:
            counts["overrides.warnings"] = len(warned)
        record_rows: dict[str, list[dict[str, Any]]] = {}
        for rec in produced:  # the same selected evidence rows, the record's own overrides
            rec_entries, _ = active_overrides(override_rows, rec)
            try:
                record_rows[rec] = select(REGISTRY[rec], docs, evidence, rec_entries, config_digest=cfg,
                                          run_id=ctx.run_id, now=now, unreached=unreached, accepted=accepted,
                                          same_basis=True)
            except RecordMissing as exc:
                raise StageError(f"{ctx.stage}: code bug: {exc}") from exc
    counts["rows"] = len(rows)
    counts["overridden"] = sum(bool(r["override_ids"]) for r in rows)
    for rec, rrows in record_rows.items():
        counts[f"rows.{rec}"] = len(rrows)
        counts[f"overridden.{rec}"] = sum(bool(r["override_ids"]) for r in rrows)
    if not spec.evidence:  # a pure workflow (V8): its invocations are recomputable, never kept
        rows = [r | {"evidence_id": None} for r in rows]
    ctx.emit_full(wf, pa.Table.from_pylist(rows, schema=family(wf).schema))
    for rec, rrows in record_rows.items():
        ctx.emit_full(rec, pa.Table.from_pylist(rrows, schema=family(rec).schema))
    if spec.evidence:
        evidence = sorted(evidence, key=lambda e: e["evidence_id"])  # the order a view reads back
        ctx.emit_full(ev_family, pa.Table.from_pylist(evidence, schema=family(ev_family).schema))
    return StageResult(scope_complete=not unreached, rows_touched=len(new_ev))


def _records(answer: Answer, produced: tuple[str, ...]) -> dict[str, Mapping[str, Any] | None]:
    """The record payloads of one answer, checked against the pin's records: a record the pin
    does not produce, or a produced record without a payload, is a code bug
    of the workflow (:class:`StageError`)."""
    got = dict(answer.records)
    extra = sorted(set(got) - set(produced))
    missing = [r for r in produced if got.get(r) is None]
    if extra or missing:
        raise StageError(f"code bug: answer records {sorted(got)} under a pin producing {list(produced)} "
                         f"(extra {extra}, missing {missing})")
    return got


def auth_stop(outcome: object) -> bool:
    """An auth/credit stop: HTTP 401/402/403 (the key's credit limit, D328's outer bound), or that
    terminal error re-raised to the calls in flight. Not the document's failure, so its rows get no
    evidence row and no attempt (review L5 LOW, D430): ``not_attempted[halted]``, asked again by
    the next run, where an evidence row's ``attempts + 1`` would bring them to the cap of 3."""
    return isinstance(outcome, FatalLlmError) and outcome.error_class == "provider_auth"


def _failure_row(spec: PayloadSpec, exc: LlmError, common: Mapping[str, Any]) -> dict[str, Any]:
    """The evidence row of a failed invocation (or tier): the error's ``(status, error_class)``."""
    return evidence_row(spec, status=exc.status, error_class=exc.error_class, error=str(exc)[:2000],
                        payload=None, llm=None, origin="ai_check", cost_usd=getattr(exc, "cost_usd", None),
                        **common)


def _workers(options: Mapping[str, Any]) -> int:
    raw = options.get("workers", DEFAULT_WORKERS)
    n = int(raw)
    if not 1 <= n <= MAX_WORKERS:
        raise StageError(f"options.workers must be between 1 and {MAX_WORKERS}, got {raw!r}")
    return n


def _cost(answer: Answer) -> float:
    total = 0.0
    for a in (answer, *answer.followups):
        llm = a.llm if isinstance(a, Answer) else None
        c = getattr(a, "cost_usd", None) if isinstance(a, LlmError) else (
            None if llm is None else llm.cost_usd if hasattr(llm, "cost_usd") else dict(llm).get("cost_usd"))
        total += float(c or 0.0)
    return total


class AskProgress:
    """The ask substage's status line on stderr (v3's screen ``_Progress``): a redrawn line on a
    TTY, else a plain line at most every 10 s (and a heartbeat while every worker is in flight).
    ``STACK_PROGRESS=0`` silences it."""

    def __init__(self, total: int, workers: int, label: str = "annotate") -> None:
        self.total, self.workers, self.label = total, workers, label
        self.active = 0
        self.failed = 0
        self.spent = 0.0
        self.started = time.monotonic()
        self.last = float("-inf")
        self.silent = os.environ.get("STACK_PROGRESS", "").lower() in ("0", "off", "false")

    def add(self, outcome: Answer | LlmError | None) -> None:
        if isinstance(outcome, LlmError):
            self.failed += 1
            self.spent += float(getattr(outcome, "cost_usd", 0.0) or 0.0)
        elif outcome is not None:
            self.spent += _cost(outcome)

    def show(self, saved: int, failed: int, spend: float, *, force: bool = False, final: bool = False) -> None:
        if self.silent:
            return
        tty = sys.stderr.isatty()
        now = time.monotonic()
        if not force and now - self.last < (1 if tty else 10):
            return
        self.last = now
        elapsed = max(now - self.started, 0.001)
        rate = saved / elapsed
        eta = f"{(self.total - saved) / rate / 3600:.1f}h" if rate else "--"
        filled = int(20 * (saved / self.total if self.total else 1))
        bar = "=" * filled + " " * (20 - filled)
        line = (f"[{self.label} {bar}] {saved}/{self.total} saved | {self.active}/{self.workers} in flight | "
                f"{rate * 60:.1f}/min | ${spend:.4f} | ETA {eta} | {failed} failed")
        end = "\n" if (final or not tty) else ""
        print(("\r\033[2K" if tty else "") + line, end=end, file=sys.stderr, flush=True)


def _text_loader(ctx: StageContext, row: Mapping[str, Any], kind: str) -> Callable[[], str] | None:
    """The lazy text of a ``text`` or ``chained`` item: the row's current text blob (a
    ``chained`` basis digest is not a blob digest)."""
    if kind not in ("text", "chained"):
        return None
    return functools.partial(_text, ctx, row["content"]["current_text_sha256"])


def _text(ctx: StageContext, digest: str) -> str:
    """The current text (``text`` blob of ``digest``); a missing blob fails the run loudly (the
    store is inconsistent, X5's business, and no ask may run on another text)."""
    from stack.store.blobs import BlobNotFound

    try:
        return ctx.blobs.get(digest).decode("utf-8")
    except BlobNotFound as exc:
        raise StageError(f"{ctx.stage}: text blob {digest} is missing from the store") from exc


__all__ = ["CLOCK", "DEFAULT_WORKERS", "MAX_WORKERS", "OPTIONS", "PIN_DIGEST_KEYS", "PURE_SUBSTAGES", "SLEEP", "SUBSTAGES", "TRANSPORT_HOOK",
           "DEFAULT_ROLES", "Answer", "AskContext", "AskItem", "AskProgress", "PromptPin", "Workflow", "auth_stop",
           "check_accepted", "config_digest", "config_name", "pin_record", "recorded_pins", "role_params",
           "pinned_records", "resolve_pin", "run_annotate", "stack_config"]
