"""``stack eval <task>``: run the production workflow code on a frozen eval set, model overridden per
run, through the ledger; one row per (model, item, repeat), then :mod:`stack.evals.score`.

**Production code, not a re-implementation.** Per task the harness calls:

* triage — :func:`workflows.triage.ask.resolve` on a question pinned ``{name, model[, role]}``
  (the pin of the plan's gate question clause: its ``prompt:`` names the ``prompts`` key, e.g.
  ``triage.safety-relevance-v3``; ``--prompt`` overrides the pin's name; a plan with no question
  clause, or with several distinct prompt keys, refuses unless ``--prompt`` is given, and then the
  pin is ``{name: --prompt}`` under :data:`PLAN_PROMPT_KEY`'s key) → ``QuestionPrompt``; then
  :func:`workflows.triage.ask._complete` on a ``_Pair`` whose ``user`` is the frozen
  ``input.record`` verbatim (the production :func:`~workflows.triage.ask.document_text` output);
* screen — :func:`workflows.annotate.screen.main.ask` with an ``AskItem`` whose text is the frozen
  whole text (``<set>/texts/<item_id>.txt.gz``, the string ``stack.annotate.stage._text`` returned at
  freeze time), an ``AskContext`` pinned ``{name, model, adjudicate_model, tier2}`` (resolved by
  ``tiers``/``prompt``), so tier 1's truncation, tier 2's whole text, the tier-2 preamble,
  ``_payload`` and ``_anchor_quotes`` (against the whole text) are production's; the pin name also
  selects the output schema (``main.schema_for``), and a row carries the Shallow Review scope fields
  ``scope``, ``sr_category``, ``sr_agenda`` of the final tier's parsed answer and ``tier1_scope``
  (None for prompts without them: the ``SCREEN`` payload has no scope columns); and the
  per-question answers (D???(E), :meth:`ScreenAdapter.questions`): ``question_mode`` (``dual``
  for ``screen-v10``, ``stack`` for ``screen-stack-v1`` and v7/v8, ``sr`` for ``screen-sr-v1``,
  ``scope`` for ``screen-v9``) and, from the final tier's parsed answer, ``stack_verdict``,
  ``stack_confidence``, ``stack_quotes_returned``, ``stack_quotes_kept``, ``sr_verdict``,
  ``sr_confidence``, ``sr_quotes_returned``, ``sr_quotes_kept``, ``sr_category``, ``sr_agenda``
  (each question's quotes anchored against the whole text with production's ``_anchor_quotes``
  and ``Anchorer``); a single-question prompt leaves the other side null; ``scope`` mode derives
  the two verdicts as gold does (:func:`stack.evals.gold.question_verdicts`), its per-question
  confidences and quotes null; ``usability_verdict``, ``usability_issues``, ``usability_note`` of the
  final tier's ``usability`` answer (``screen-v11``; null otherwise); ``chars_sent_tier1`` /
  ``chars_sent_tier2`` = the document characters each tier's screening text showed
  (``main.screening_text`` under the pin's ``main.input_limits``; tier 2 null when not asked).
  ``--pin KEY=JSON`` (repeatable) sets a plan-pin key of every model's pin (``tier1_chars``,
  ``tier2_chars``, …; ``name``, ``model``, ``adjudicate_model`` and ``tier2`` have their own options
  and are refused); the plan's own ``tier1_chars`` / ``tier2_chars`` apply when the run's prompt is
  the plan pin's name; ``run.json`` records ``pin_overrides`` and the effective pins;
* affiliation — the LLM path of the v3 runner ``workflows/annotate/affiliation/main.py`` (loaded by
  path, :func:`stack.evals.gold.affiliation_runner`): role and template as ``run`` takes them from
  the ``annotate.affiliation`` registration (``llm_role``; ``<prompt basename>.md`` beside main.py),
  ``refs={}``, ``user = _user_message(build_authors_block(_roster(authors_meta)), window)``,
  ``schema = extract.RESPONSE_SCHEMA``, plus ``model=<id>``; then ``parse_authors`` →
  ``align_to_roster`` → ``resolve`` (no OpenAlex tier, as no ``authors_detail`` is frozen). The XML
  tier is not run: the eval measures the model on the text; rows carry ``production_uses_llm``
  (false where production's XML tier would answer instead). Each returned surface is also run
  through production's ``anchor_payload`` against the frozen window (production anchors against
  the whole text, which contains the window); voided surfaces are counted, not removed;
* model_roles, technique_roles — production ``main.ask`` (``workflows/annotate/<task>/main.py``) with
  the frozen whole text, pinned ``{name, model}`` (:mod:`stack.evals.annotators`: the AskItem, the
  no-call items, the row fields ``ann_status`` / ``payload`` / ``counts`` / ``no_call``); ``--split
  all`` takes every item. ``stack eval model_mentions`` (no LLM, no ledger) is
  :func:`stack.evals.annotators.run_mentions`;
* technique_staged — production ``workflows/annotate/technique_staged/main.ask`` in the pin's ``mode``
  (:class:`stack.evals.annotators.TechniqueStagedAdapter`, techpipe D607): ``--models`` is the grading
  model, ``--pin KEY=VALUE`` the other pin keys (``mode`` required); several calls per row, so the
  adapter overrides the client (P and S responses in a cache shared across arms within a repeat, the
  final call in the arm's own), the per-row recorder and accounting (``calls``, ``standalone_cost_usd``)
  and the estimate (:meth:`Adapter.open_client`, :meth:`Adapter.recorder`, :meth:`Adapter.accounting`,
  :meth:`Adapter.estimate`); scored by :mod:`stack.evals.technique_staged`;
* all — :func:`stack.llm.open_client` (ledger reservation per attempt, response cache, egress
  gate, retries, strict-schema degrade) and :class:`stack.llm.BudgetScope`.

**Input check.** Before any call every item's user message is recomputed with production code
(triage: ``document_text`` from the frozen title/authors/abstract/excerpt; screen: the tier-1
``user`` that ``ask`` passes to the client, captured by a probe client that never sends;
affiliation: the runner's glue on the frozen window and ``authors_meta``; model_roles /
technique_roles: the ``user`` ``ask`` passes to the client, captured as screen's; an item frozen
without a call, ``input_digest`` null, must still make none) and its sha256 is
compared with the item's ``input_digest``; also the frozen copies hash to their digests (triage's
``input.record``, affiliation's ``input.user``, screen's whole text to ``input.full_text_digest``).
Any difference refuses the run (production code moved on from the frozen set: a new set is needed).
Screen's user-message comparison applies only when every pin's tier-1 limit is the frozen one
(``main.LEGACY_LIMITS``' 8,000-character head, as the set was frozen); under another limit the
message differs by design and only the whole-text digest is checked (``run.json`` ``input_check``).

**Clients.** One per (model, repeat): ``open_client(config, run_id=<eval run id>,
stage="eval.<task>", store_root=<cache root>/<model slug>/r<k>, budget_usd=--max-usd)``. The cache
root is ``<out>/<task>/<set>/cache`` (per set, not per run dir), so repeat k makes fresh calls and
re-running the same command is served from the cache at $0 (technique_staged: ``<cache root>/shared/r<k>``
for P and S, ``<cache root>/arm-<mode>-<model>-<pin digest>/r<k>`` for the final call). The ledger is the eval run's own, in
memory (:func:`stack.llm.ledger.run_ledger`); ``--max-usd`` is the run's budget across all
models (one run id), and ``run.json`` ``spend`` is the run's spend
(:func:`stack.llm.ledger.close_run`). The egress overlay (:mod:`stack.evals.overlay`) is applied in memory.

**Order and failures.** Calls go in (repeat, item, model) order through a pool of ``--workers``
(so a budget trip leaves every model with the same item prefix). A ``BudgetExceeded`` makes that row and every later one ``not_attempted[budget]`` (calls in
flight finish), the run ends ``ok`` with a partial summary; a ``FatalLlmError`` is its row's
failure and every later row ``not_attempted[halted]`` (run ``halted``); an item whose licence
class the gate refuses for a model is ``skipped[egress]`` with no call (pre-checked with the
client's own ``_gated_models``; ``EgressRefused`` is caught as a backstop; a NULL licence class is
``restricted``, as production passes it, :func:`license_of`); any other
``LlmError`` is the row's ``(status, error_class)``; any other exception fails the run.

**Gold.** ``--gold <file>``, else ``gold-v2.jsonl`` when the set has it, else ``gold.jsonl``; the
choice is recorded as ``run.json`` ``gold`` and :func:`stack.evals.score.score_run` scores with it.

**Estimate.** Before a run (and alone with ``--estimate``): per planned call, the reservation the
client would make (``stack.llm.reserve_tokens_in`` on the real first-attempt request body +
``max_tokens``, at the ``llm.yaml`` price) and an expected cost (output ≈ 150 tokens triage,
≈ 400 screen, ≈ 500 affiliation, ≈ 1,500 model_roles, ≈ 3,000 technique_roles; :data:`EXPECTED_OUT_TOKENS`);
calls already in the response cache count $0, an item production answers without a call $0. Screen's tier 2 enters the
reservation sum at an assumed escalation share (:data:`TIER2_SHARE_ASSUMED`; the all-escalate
upper bound is reported beside it). A reservation sum above ``--max-usd`` refuses the run.
"""

from __future__ import annotations

import datetime as dt
import functools
import hashlib
import json
import re
import subprocess
import sys
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from stack.core import hashes
from stack.evals import overlay as egress_overlay
from stack.llm import (
    BudgetScope,
    EgressRefused,
    FatalLlmError,
    LlmClient,
    LlmError,
    LlmRequest,
    LlmResponse,
    LlmResult,
    open_client,
    render_call,
    reserve_tokens_in,
    wire_max_tokens,
)
from stack.llm import DEFAULT_WORKERS as LLM_DEFAULT_WORKERS
from stack.llm import MAX_WORKERS as LLM_MAX_WORKERS
from stack.llm.cache import ResponseCache, cache_key
from stack.llm.ledger import Ledger, close_run, forget_run

if TYPE_CHECKING:
    from stack.config import LlmConfig, RoleConfig, StackConfig
    from stack.plan import Plan

Row = dict[str, Any]

TASKS = ("triage", "screen", "affiliation", "model_roles", "technique_roles", "technique_staged")
#: The annotator tasks (:mod:`stack.evals.annotators`); they also take ``--split all``.
ANNOTATOR_TASKS = ("model_roles", "technique_roles", "technique_staged")
DEFAULT_PROMPT = {"triage": "safety-relevance-v1", "screen": "screen-v7", "model_roles": "model-roles-v2",
                  "technique_roles": "technique-roles-v2", "technique_staged": "technique-staged-v1"}
#: The affiliation workflow's registration id (its ``llm_role`` and ``prompt``).
AFFILIATION_WORKFLOW = "annotate.affiliation"
#: The affiliation prompt's pin name and role, as v3's ``workflows.yaml`` registration gave them
#: (``prompt``, ``llm_role``) until its retirement at T4.5 (D42); kept as constants so eval pins and
#: earlier results stay comparable. Production pins ``role: extract_main`` in the plans
#: (``main.DEFAULT_ROLE``, max_tokens 8192, one fallback); the eval keeps ``extract_cheap``.
AFFILIATION_PROMPT = "annotate.affiliation/extract"
AFFILIATION_ROLE = "extract_cheap"
#: The plan prompt key whose pin names the production prompt (``--prompt`` overrides its name); triage
#: takes its key from the plan's gate question clause (:func:`triage_prompt_key`), this one only when
#: the plan has none and ``--prompt`` is given.
PLAN_PROMPT_KEY = {"triage": "triage.safety-relevance-v1", "screen": "annotate.screen",
                   "technique_staged": "annotate.technique_staged"}
#: Expected output tokens per call. model_roles / technique_roles: rough guesses (T-ANN R2's largest
#: model_roles reply was 10,718 tokens on a 30-node menu, typical replies far shorter); the reservation
#: uses production's ``MAX_TOKENS`` (16,000) regardless.
EXPECTED_OUT_TOKENS = {"triage": 150, "screen": 400, "affiliation": 500, "model_roles": 1500,
                       "technique_roles": 3000, "technique_staged": 3000}
#: Screen tier 2's share of items assumed by the estimate's reservation sum (v3's tier-1 model said
#: borderline on 8 %; 0.3 leaves headroom). The ledger enforces ``--max-usd`` live regardless.
TIER2_SHARE_ASSUMED = 0.3
DEFAULT_MAX_USD = 1.0
#: The repository root (``stack/evals/harness.py`` → ``../..``): the default eval sets and outputs are
#: the repository's whatever the cwd (S25-96, the eval half of S-6; as ``stack.cli.common.DEFAULT_STORE``).
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "v4" / "evals-store"
DEFAULT_EVALS = REPO_ROOT / "evals"
MAX_WORKERS = LLM_MAX_WORKERS  # 1024 since D716 (256 since D692, was 32): stack.llm's bound, the stages' too
DEFAULT_WORKERS = LLM_DEFAULT_WORKERS  # 64 since D692 (was 8): --workers when not given
SPEND_FILE = "spend.jsonl"


class EvalError(Exception):
    """An expected refusal (bad options, a moved input, an estimate over budget)."""


# ── the set ──────────────────────────────────────────────────────────────────────────────────


def read_jsonl(path: Path) -> list[Row]:
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, rows: Sequence[Row]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows),
                    "utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def read_frozen_text(set_dir: Path | None, item: Row) -> str:
    """The item's frozen whole text (``<set>/texts/<item_id>.txt.gz``, else the eval-text cache's copy,
    :func:`stack.evals.freeze.find_text`, D790); a set frozen without whole texts (no
    ``input.full_text_digest``) or a text in neither place refuses, naming the fetch tool. The digest is
    checked by the adapters' ``frozen_problems`` (and by :func:`stack.evals.annotators.run_mentions`)."""
    from stack.evals.freeze import find_text, missing_text_message, read_text_gz

    iid = item["item_id"]
    if not item["input"].get("full_text_digest"):
        raise EvalError(f"{iid}: no input.full_text_digest: the set was frozen without the whole text (re-freeze it)")
    if set_dir is None:
        raise EvalError("the adapter has no set directory")
    path = find_text(set_dir, iid)
    if path is None:
        raise EvalError(missing_text_message(set_dir, iid))
    return read_text_gz(path)


def license_of(item: Row) -> str:
    """The item's licence class as production hands it to the client: a NULL class is ``restricted``
    (``stack/annotate/stage.py``, ``workflows/triage/ask.py``; S-41). Every adapter's calls and the
    egress pre-check use it; the row records the frozen value."""
    return str(item.get("license_class") or "restricted")


@dataclass(frozen=True)
class EvalSet:
    task: str
    set_id: str
    dir: Path
    items: list[Row]
    manifest_digest: str | None
    items_digest: str


def load_set(evals_root: Path, task: str, set_id: str) -> EvalSet:
    from stack.evals.freeze import ARCHIVE_NOTE, FETCH_TEXTS_HINT, items_file

    d = evals_root / task / set_id
    path = items_file(d)
    if not path.is_file():
        if (d / "manifest.json").is_file():
            raise EvalError(f"eval set {d}: items.jsonl is in neither the set nor the eval-text cache (D795; "
                            f"`{FETCH_TEXTS_HINT}` fills it; {ARCHIVE_NOTE})")
        raise EvalError(f"no eval set {path}")
    items = read_jsonl(path)
    ids = [i["item_id"] for i in items]
    if len(set(ids)) != len(ids):
        raise EvalError(f"{path}: duplicate item_id")
    return EvalSet(task, set_id, d, items, sha256_file(d / "manifest.json"), sha256_file(path) or "")


#: ``--split all``: every item of the set (annotator tasks, :data:`ANNOTATOR_TASKS`, and model_mentions).
ALL_SPLIT = "all"


def select_items(items: Sequence[Row], split: str, ids: Sequence[str] | None, limit: int | None) -> list[Row]:
    """The split's items (:data:`ALL_SPLIT`: every item) in ``item_id`` order, narrowed to ``ids``
    (each must be in the split) and then to the first ``limit``."""
    chosen = sorted((i for i in items if split == ALL_SPLIT or i["split"] == split), key=lambda i: i["item_id"])
    if ids:
        by = {i["item_id"]: i for i in chosen}
        missing = [x for x in ids if x not in by]
        if missing:
            raise EvalError(f"--items {missing} are not {split!r} items of the set")
        chosen = [by[x] for x in sorted(set(ids))]
    if limit is not None:
        chosen = chosen[:limit]
    if not chosen:
        raise EvalError(f"no {split!r} items selected")
    return chosen


# ── capturing what production code sends, without sending ───────────────────────────────────


class _Captured(Exception):
    """Raised by :class:`_CaptureClient` once it has the call it was asked to capture."""


class _CaptureClient:
    """A stand-in for ``LlmClient`` that records the keyword arguments of each ``complete`` call
    and never sends: calls before the last captured one get a canned answer (``answer``, e.g. a
    tier-1 ``borderline`` to elicit screen's tier 2), the last raises :class:`_Captured`."""

    def __init__(self, calls: int, answer: Mapping[str, Any] | None = None) -> None:
        self.calls = calls
        self.answer = dict(answer or {})
        self.captured: list[dict[str, Any]] = []

    def complete(self, **kw: Any) -> Any:
        self.captured.append(kw)
        if len(self.captured) >= self.calls:
            raise _Captured
        return SimpleNamespace(parsed=dict(self.answer), model=kw.get("model"))


class _NoSend:
    """The estimate's transport: refuses loudly (the estimate never posts)."""

    def post(self, request: LlmRequest) -> LlmResponse:
        raise AssertionError("stack eval --estimate must not send a request")


class _Recorder:
    """Wraps the run's client for one row: every ``complete`` result or raised ``LlmError`` is
    kept, so a row's settled cost, reservations, tokens and attempts include every tier (also
    when a later tier raised)."""

    def __init__(self, client: LlmClient) -> None:
        self.client = client
        self.calls: list[LlmResult | LlmError] = []

    def complete(self, **kw: Any) -> LlmResult:
        try:
            res = self.client.complete(**kw)
        except LlmError as exc:
            self.calls.append(exc)
            raise
        self.calls.append(res)
        return res


# ── task adapters ────────────────────────────────────────────────────────────────────────────


@dataclass
class Adapter:
    """What differs per task: the pin, the input check, the egress role, the calls, the row."""

    task: str
    config: StackConfig
    prompt_name: str
    models: list[str]
    run_id: str = ""
    set_dir: Path | None = None  # the set directory (screen, the annotators read frozen whole texts there)
    cache_root: Path | None = None  # ``<out>/<task>/<set>/cache`` (set by :func:`run_eval`)
    _texts: dict[str, str] = field(default_factory=dict, repr=False)
    _texts_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def pins(self) -> dict[str, Any]:
        raise NotImplementedError

    def full_text(self, item: Row) -> str:
        """The item's frozen whole text (:func:`read_frozen_text`), read once."""
        iid = item["item_id"]
        with self._texts_lock:
            if iid in self._texts:
                return self._texts[iid]
        text = read_frozen_text(self.set_dir, item)
        with self._texts_lock:
            self._texts[iid] = text
        return text

    def frozen_problems(self, item: Row) -> list[str]:
        """Frozen copies in the item that do not hash to their recorded digests."""
        return []

    def checks_user_message(self) -> bool:
        """Whether the frozen ``input_digest`` is the user message this run sends (module docstring)."""
        return True

    def tier1_role(self, model: str) -> RoleConfig:
        raise NotImplementedError

    def user_message(self, item: Row) -> str:
        raise NotImplementedError

    def capture(self, item: Row, model: str, *, tier2: bool) -> dict[str, Any] | None:
        """The ``complete`` kwargs of the item's tier-1 call (``tier2``: of its tier-2 call, or
        None when the task has no second tier)."""
        raise NotImplementedError

    def run_one(self, item: Row, model: str, client: _Recorder) -> Row:
        raise NotImplementedError

    def gold_problems(self, gold: Mapping[str, Row]) -> list[str]:
        """Why ``gold`` (by item_id) cannot be scored against what production can answer now, e.g.
        a gold id absent from the menu (:meth:`stack.evals.annotators.AnnotatorAdapter.gold_problems`);
        :func:`run_eval` refuses on any, before any call."""
        return []

    def provenance(self) -> Row:
        """Task-specific ``run.json`` fields, read once :attr:`set_dir` is set; a ``warnings`` list in
        them is also printed (``warning: …`` on stderr) and repeated in the summary (model_roles: the
        frozen and current vocabulary digests, :meth:`stack.evals.annotators.AnnotatorAdapter.provenance`)."""
        return {}

    # Hooks a task with several calls per row overrides (technique_staged,
    # :class:`stack.evals.annotators.TechniqueStagedAdapter`: its cache layout, per-call rows, estimate).

    def open_client(self, cfg: StackConfig, *, run_id: str, stage: str, model: str, repeat: int,
                    store_root: Path, budget_usd: float) -> Any:
        """The client of one (model, repeat): ``open_client`` on ``store_root`` (module docstring, "Clients")."""
        return open_client(cfg, run_id=run_id, stage=stage, store_root=store_root, budget_usd=budget_usd)

    def recorder(self, client: Any) -> _Recorder:
        """The per-row wrapper of the run's client."""
        return _Recorder(client)

    def accounting(self, rec: _Recorder) -> Row:
        """A row's cost, token, latency and attempt fields from its recorder (:func:`_accounting`)."""
        return _accounting(rec)

    def estimate(self, items: Sequence[Row], models: Sequence[str], repeat: int, *, probe: LlmClient,
                 cache_dir: Callable[[str, int], Path]) -> Row:
        """The run's estimate (:func:`estimate`)."""
        return estimate(self, items, models, repeat, probe=probe, cache_dir=cache_dir)

    def format_estimate(self, est: Row, max_usd: float) -> str:
        return format_estimate(est, max_usd)


def triage_prompt_key(plan: Plan | None, prompt: str | None) -> tuple[str, str, dict[str, Any]]:
    """``(question, prompts key, pin)`` the triage eval asks (module docstring): the plan's gate
    question clause's; with none or several distinct prompt keys, ``--prompt`` is required and the
    pin is empty (its name comes from ``--prompt``)."""
    from stack import gate

    try:
        qs = gate.questions(plan) if plan is not None else []
    except gate.GateError as exc:
        raise EvalError(str(exc)) from exc
    keys = list(dict.fromkeys(q.prompt for q in qs))
    if len(keys) == 1:
        q = qs[0]
        return q.name, q.prompt, dict(plan.prompts.get(q.prompt) or {}) if plan is not None else {}
    if prompt is None:
        what = (f"several distinct question prompts {keys}" if keys else "no gate question clause")
        raise EvalError(f"triage: the plan{' ' + plan.plan if plan is not None else ''} has {what}; "
                        f"name the prompt with --prompt (e.g. --prompt {DEFAULT_PROMPT['triage']})")
    return "safety_relevance", PLAN_PROMPT_KEY["triage"], {}


class TriageAdapter(Adapter):
    def __init__(self, config: StackConfig, plan: Plan | None, prompt: str | None, models: list[str]) -> None:
        from stack import gate
        from workflows.triage import ask as triage

        qname, self.prompt_key, pin = triage_prompt_key(plan, prompt)
        name = prompt or pin.get("name") or DEFAULT_PROMPT["triage"]
        super().__init__("triage", config, str(name), models)
        self._t = triage
        self.prompts: dict[str, Any] = {}
        for m in models:
            q_pin: dict[str, Any] = {"name": name, "model": m}
            if pin.get("role"):
                q_pin["role"] = pin["role"]
            q = gate.Question(qname, self.prompt_key, q_pin)
            try:
                self.prompts[m] = triage.resolve(q, config.llm)
            except gate.GateError as exc:
                raise EvalError(str(exc)) from exc

    def pins(self) -> dict[str, Any]:
        return {m: {"name": p.name, "model": p.model, "role": p.role, "template_digest": p.template_digest,
                    "prompt_key": self.prompt_key} for m, p in self.prompts.items()}

    def tier1_role(self, model: str) -> RoleConfig:
        qp = self.prompts[model]
        return self.config.llm.roles[qp.role].model_copy(update={"model": qp.model})

    def user_message(self, item: Row) -> str:
        inp = item["input"]
        row = {"metadata": {"title": inp.get("title"), "abstract": inp.get("abstract"),
                            "authors": [{"name": n} for n in inp.get("authors") or []]}}
        return self._t.document_text(row, inp.get("excerpt"))

    def _pair(self, item: Row, model: str) -> Any:
        return self._t._Pair(row={"doc_id": item["doc_id"], "metadata": {"fields_digest": item["input_digest"]}},
                             prompt=self.prompts[model], source_id=item["doc_id"],
                             license_class=license_of(item), prev=None, user=item["input"]["record"])

    def capture(self, item: Row, model: str, *, tier2: bool) -> dict[str, Any] | None:
        if tier2:
            return None
        probe = _CaptureClient(1)
        try:
            self._t._complete(probe, self._pair(item, model))  # type: ignore[arg-type]
        except _Captured:
            return probe.captured[-1]
        raise AssertionError("triage _complete returned without calling the client")

    def frozen_problems(self, item: Row) -> list[str]:
        if sha256_text(item["input"]["record"]) != item["input_digest"]:
            return ["input.record does not hash to its input_digest"]
        return []

    def run_one(self, item: Row, model: str, client: _Recorder) -> Row:
        res = self._t._complete(client, self._pair(item, model))  # type: ignore[arg-type]
        p = res.parsed
        return {"verdict": str(p["verdict"]), "confidence": float(p["confidence"]),
                "reasoning": str(p["reasoning"])}


class ScreenAdapter(Adapter):
    #: Plan-pin keys ``--pin`` may not set (each has its own option).
    OWN_OPTION_KEYS = {"name": "--prompt", "model": "--models", "adjudicate_model": "--tier2-model",
                       "tier2": "--tier2/--no-tier2"}
    #: Plan-pin keys carried from the plan's pin when the run's prompt is the plan pin's name.
    PLAN_KEYS = ("tier1_chars", "tier2_chars")

    def __init__(self, config: StackConfig, plan: Plan | None, prompt: str | None, models: list[str], *,
                 tier2: bool, tier2_model: str | None, pin_overrides: Mapping[str, Any] | None = None) -> None:
        from workflows.annotate.screen import main as screen

        pin = dict((plan.prompts.get(PLAN_PROMPT_KEY["screen"]) if plan is not None else None) or {})
        name = prompt or pin.get("name") or DEFAULT_PROMPT["screen"]
        overrides = dict(pin_overrides or {})
        own = sorted(k for k in overrides if k in self.OWN_OPTION_KEYS)
        if own:
            raise EvalError(f"--pin {own}: set with {', '.join(self.OWN_OPTION_KEYS[k] for k in own)} instead")
        carried = {k: pin[k] for k in self.PLAN_KEYS if k in pin and pin.get("name", DEFAULT_PROMPT["screen"]) == name}
        self.pin_overrides = overrides
        super().__init__("screen", config, str(name), models)
        # The pin name selects the template (``screen-v7`` → ``screen.md``, else
        # ``workflows/annotate/screen/<name>.md``) and the output schema (``main.schema_for``: the SR
        # scope fields for a name in question mode ``scope``) inside ``ask``/``prompt`` (D179 (C)); resolving it
        # here refuses a missing template before any call. The ``TEMPLATE`` assignment (D175 (E)) is
        # kept for importers of the attribute; production no longer reads it.
        try:
            screen.TEMPLATE = screen.template(name)
        except Exception as exc:  # StageError: no such template / not a template name
            raise EvalError(f"screen: --prompt {name!r}: {exc}") from exc
        self._s = screen
        self.tier2 = tier2
        self.tier2_model = (tier2_model or config.llm.roles[screen.ADJUDICATE_ROLE].model) if tier2 else None
        self.question_mode = screen.question_mode(name)
        self._anchorer: Any = None
        self.plan_pins: dict[str, dict[str, Any]] = {}
        for m in models:
            p: dict[str, Any] = {"name": name, "model": m, "tier2": tier2}
            if tier2:
                p["adjudicate_model"] = self.tier2_model
            p.update(carried)
            p.update(overrides)
            try:
                screen.prompt(p, config.llm)
            except Exception as exc:  # StageError: a bad pin (an unknown key, a bad limit)
                raise EvalError(str(exc)) from exc
            self.plan_pins[m] = p

    def pins(self) -> dict[str, Any]:
        out = {}
        for m, p in self.plan_pins.items():
            pp = self._s.prompt(p, self.config.llm)
            out[m] = {**p, "template_digests": list(pp.template_digests), "models": list(pp.models)}
        return out

    def tier1_role(self, model: str) -> RoleConfig:
        return self.config.llm.roles[self._s.ROLE].model_copy(update={"model": model})

    def checks_user_message(self) -> bool:
        legacy = self._s.LEGACY_LIMITS[0]
        return all(self._s.input_limits(p).tier1 == legacy for p in self.plan_pins.values())

    def frozen_problems(self, item: Row) -> list[str]:
        got = sha256_text(self.full_text(item))
        if got != item["input"]["full_text_digest"]:
            return [f"whole text (texts/{item['item_id']}.txt.gz) hashes to {got[:12]}, frozen "
                    f"full_text_digest {item['input']['full_text_digest'][:12]}"]
        return []

    def _ask_args(self, item: Row, model: str, client: Any) -> tuple[Any, Any]:
        from stack.annotate.stage import AskContext, AskItem

        inp = item["input"]
        text = self.full_text(item)
        ask_item = AskItem(
            doc_id=item["doc_id"], source_id=item["doc_id"], basis_kind="text",
            basis_digest=str(inp.get("text_digest") or item["input_digest"]), fields=None,
            row={"doc_id": item["doc_id"], "metadata": {"title": inp.get("title"),
                                                        "doc_type": self.collector_doc_type(item)},
                 "content": {"current_role": inp.get("basis_role")}},
            license_class=license_of(item), attempt=1, load_text=lambda: text)
        plan_pin = self.plan_pins[model]
        cfg = self.config
        actx = AskContext(run_id=self.run_id, stage="eval.screen", store_root=None,
                          pin=self._s.prompt(plan_pin, cfg.llm), plan_pin=plan_pin, options={},
                          budget_usd=None, _config=lambda: cfg, _client=client)
        return ask_item, actx

    def capture(self, item: Row, model: str, *, tier2: bool) -> dict[str, Any] | None:
        if tier2 and not self.tier2:
            return None
        # Tier 2 is elicited with a tier-1 `borderline` and a 400-char rationale (the cap ask applies);
        # the canned answer carries every per-question object a record reads (screen-v10, v11).
        side = {"verdict": "borderline", "confidence": 0.5, "quotes": []}
        probe = _CaptureClient(2 if tier2 else 1, {
            "verdict": "borderline", "rationale": "x" * 400, "buckets": [], "quotes": [],
            "stack": side, "sr": {**side, "category": "none", "agenda": None},
            "usability": {"verdict": "usable", "issues": [], "note": None},
            "doc_class": {"doc_type": "other", "self_published": False, "primary_lang": None}})
        try:
            self._s.ask(*self._ask_args(item, model, probe))
        except _Captured:
            return probe.captured[-1]
        raise AssertionError("screen ask returned without making the captured call")

    def user_message(self, item: Row) -> str:
        kw = self.capture(item, self.models[0], tier2=False)
        assert kw is not None
        return str(kw["user"])

    def run_one(self, item: Row, model: str, client: _Recorder) -> Row:
        from stack.annotate.stage import Answer

        ans = self._s.ask(*self._ask_args(item, model, client))
        if ans is None:  # only a tier-2 allowance (stage options) defers; the harness passes no options
            raise EvalError(f"{item['item_id']}: screen ask deferred tier 2 under the harness's empty options")
        p1 = ans.payload
        final, tier2_err = p1, None
        final_records = ans.records
        if ans.followups:
            f = ans.followups[0]
            if isinstance(f, Answer):
                final, final_records = f.payload, f.records
            else:
                tier2_err = f
        results = [c for c in client.calls if isinstance(c, LlmResult)]

        def returned(res: LlmResult | None) -> int:
            # the quotes ``_payload`` anchors (screen-v10: both questions', exact repeats once)
            return len(self._s.answer_quotes(res.parsed)) if res is not None else 0

        r1 = results[0] if results else None
        r2 = results[1] if len(results) > 1 else None
        kept1 = len(p1.get("quotes") or [])
        kept2 = len(final.get("quotes") or []) if final is not p1 else 0
        # The SR scope fields (prompts in question mode ``scope``) are not in the ``SCREEN``
        # payload, so they come from the parsed answers: the final tier's (tier 2's when it
        # answered), and tier 1's scope; None where the answer has no such key (v7/v8 prompts).
        final_res = r2 if (final is not p1 and r2 is not None) else r1
        fp = final_res.parsed if final_res is not None else {}
        t1p = r1.parsed if r1 is not None else {}
        return {
            "verdict": final["verdict"], "tier1_verdict": p1["verdict"],
            "adjudication_tier": final["adjudication_tier"],
            "tier2_attempted": bool(ans.counts.get("tier2")),
            "tier2_model": self.tier2_model if ans.counts.get("tier2") else None,
            "tier2_error_class": getattr(tier2_err, "error_class", None) if tier2_err is not None else None,
            "buckets": list(final.get("buckets") or []), "off_topic_reason": final.get("off_topic_reason"),
            "confidence": final.get("confidence"), "reasoning": final.get("reasoning"),
            "quotes_kept": kept1, "quotes_voided": returned(r1) - kept1,
            "tier2_quotes_kept": kept2, "tier2_quotes_voided": (returned(r2) - kept2) if r2 is not None else 0,
            "scope": fp.get("scope"), "tier1_scope": t1p.get("scope"),
            # sr_category / sr_agenda: v9's scope fields, or the SR answer's category / agenda
            **self.questions(fp, self.full_text(item)),
            **self.usability(fp),
            **self.doc_class(fp, final_records),
            **self.chars_sent(item, model, tier2=bool(ans.counts.get("tier2"))),
        }

    def usability(self, parsed: Mapping[str, Any]) -> Row:
        """``usability_verdict`` / ``_issues`` / ``_note`` of a parsed answer's ``usability`` object
        (``screen-v11``), null when it has none."""
        u = parsed.get("usability")
        if not isinstance(u, Mapping):
            return {"usability_verdict": None, "usability_issues": None, "usability_note": None}
        issues = u.get("issues")
        return {"usability_verdict": u.get("verdict"),
                "usability_issues": [str(x) for x in issues] if isinstance(issues, list) else [],
                "usability_note": u.get("note")}

    #: The set file mapping item id → the collector's ``metadata.doc_type`` (optional; T-SCREEN).
    DOC_TYPES_FILE = "collector-doc-types.json"

    def collector_doc_type(self, item: Row) -> str | None:
        """The item's collector ``metadata.doc_type`` from the set's :data:`DOC_TYPES_FILE` (None
        when the set has none), so ``screen-v12``'s channel rule applies as in production; no
        prompt's user message reads it."""
        if not hasattr(self, "_doc_types"):
            path = self.set_dir / self.DOC_TYPES_FILE if self.set_dir is not None else None
            self._doc_types: dict[str, str] = (json.loads(path.read_text("utf-8"))["doc_types"]
                                               if path is not None and path.is_file() else {})
        return self._doc_types.get(item["item_id"])

    def doc_class(self, parsed: Mapping[str, Any], records: Mapping[str, Any]) -> Row:
        """``doc_type`` (the ``doc_class`` record's: the channel's where it decides), ``doc_type_model``
        (the final tier's answer), ``self_published``, ``primary_lang`` (``screen-v12``; null otherwise)."""
        dc, rec = parsed.get("doc_class"), records.get("doc_class") or {}
        if not isinstance(dc, Mapping):
            return {"doc_type": None, "doc_type_model": None, "self_published": None, "primary_lang": None}
        return {"doc_type": rec.get("doc_type"), "doc_type_model": dc.get("doc_type"),
                "self_published": rec.get("self_published"), "primary_lang": rec.get("primary_lang")}

    def chars_sent(self, item: Row, model: str, *, tier2: bool) -> Row:
        """The document characters each tier's screening text showed (tier 2: None unless asked)."""
        text = self.full_text(item)
        lim = self._s.input_limits(self.plan_pins[model])
        return {"chars_sent_tier1": self._s.screening_text(text, lim.tier1)[1],
                "chars_sent_tier2": self._s.screening_text(text, lim.tier2)[1] if tier2 else None}

    #: The per-question row fields (module docstring), null unless the answer gives them.
    QUESTION_FIELDS = ("stack_verdict", "stack_confidence", "stack_quotes_returned", "stack_quotes_kept",
                       "sr_verdict", "sr_confidence", "sr_quotes_returned", "sr_quotes_kept", "sr_category",
                       "sr_agenda")

    def questions(self, parsed: Mapping[str, Any], text: str) -> Row:
        """The per-question fields of one parsed answer (module docstring) under this run's
        :attr:`question_mode`."""
        from stack.evals.gold import question_verdicts

        out: Row = {"question_mode": self.question_mode, **dict.fromkeys(self.QUESTION_FIELDS)}
        mode = self.question_mode
        if mode == "scope":
            out["stack_verdict"], out["sr_verdict"] = question_verdicts(
                parsed.get("verdict"), parsed.get("scope"), parsed.get("sr_category"))
            out["sr_category"], out["sr_agenda"] = parsed.get("sr_category"), parsed.get("sr_agenda")
            return out
        sides: list[tuple[str, Mapping[str, Any]]]
        if mode == "dual":
            sides = [(q, parsed[q]) for q in ("stack", "sr") if isinstance(parsed.get(q), Mapping)]
        else:
            sides = [(mode, parsed)]
        for q, ans in sides:
            raw = ans.get("quotes")
            quotes = [str(x) for x in raw] if isinstance(raw, list) else []
            kept, _ = self._s._anchor_quotes(self.anchorer(), text, quotes)
            out.update({f"{q}_verdict": ans.get("verdict"), f"{q}_confidence": ans.get("confidence"),
                        f"{q}_quotes_returned": len(quotes), f"{q}_quotes_kept": len(kept)})
            if q == "sr":
                out["sr_category"], out["sr_agenda"] = ans.get("category"), ans.get("agenda")
        return out

    def anchorer(self) -> Any:
        """Production's screen :class:`~stack.anchor.Anchorer` (``policies.yaml`` threshold), built once."""
        if self._anchorer is None:
            from stack.anchor import Anchorer

            self._anchorer = Anchorer.from_config(self.config, workflow="annotate.screen")
        return self._anchorer


class AffiliationAdapter(Adapter):
    """The workflow's LLM path (module docstring): role :data:`AFFILIATION_ROLE`, template
    ``main.TEMPLATE`` (``extract.md``), ``complete(role=role, template=template, refs={},
    user=_user_message(...), schema=ex.RESPONSE_SCHEMA, license_class=...)``; ``_user_message``,
    ``_roster`` and everything in ``extract.py`` are called, not copied. (Until T4.5 the role and
    prompt came from v3's ``workflows.yaml`` registration, D42.)"""

    def __init__(self, config: StackConfig, prompt: str | None, models: list[str]) -> None:
        from stack.anchor import Anchorer
        from stack.canon import Canon
        from stack.evals.gold import affiliation_runner

        main = affiliation_runner()
        role = AFFILIATION_ROLE
        if prompt is not None and prompt != AFFILIATION_PROMPT:
            raise EvalError(f"affiliation: --prompt {prompt!r}: the workflow has one prompt, {AFFILIATION_PROMPT!r}")
        if role not in config.llm.roles:
            raise EvalError(f"affiliation needs llm.yaml role {role!r}")
        super().__init__("affiliation", config, AFFILIATION_PROMPT, models)
        self.main, self.ex = main, main.ex
        self.role = role
        self.template = str(main.TEMPLATE)
        self.canon = Canon.from_config(config)
        self.anchorer = Anchorer.from_config(config, workflow=AFFILIATION_WORKFLOW)

    def pins(self) -> dict[str, Any]:
        return {m: {"name": self.prompt_name, "model": m, "role": self.role,
                    "template_sha256": sha256_text(self.template),
                    "schema_sha256": sha256_text(hashes.jcs(self.ex.RESPONSE_SCHEMA))} for m in self.models}

    def tier1_role(self, model: str) -> RoleConfig:
        return self.config.llm.roles[self.role].model_copy(update={"model": model})

    def _roster(self, item: Row) -> list[str]:
        return list(self.main._roster(SimpleNamespace(extra={"authors": list(item["input"]["authors_meta"])})))

    def user_message(self, item: Row) -> str:
        block = self.ex.build_authors_block(self._roster(item))
        return str(self.main._user_message(block, item["input"]["window"]))

    def frozen_problems(self, item: Row) -> list[str]:
        if sha256_text(item["input"]["user"]) != item["input_digest"]:
            return ["input.user does not hash to its input_digest"]
        return []

    def _kwargs(self, item: Row, model: str) -> dict[str, Any]:
        return {"role": self.role, "template": self.template, "refs": {}, "user": self.user_message(item),
                "schema": self.ex.RESPONSE_SCHEMA, "license_class": license_of(item),
                "model": model}

    def capture(self, item: Row, model: str, *, tier2: bool) -> dict[str, Any] | None:
        return None if tier2 else self._kwargs(item, model)

    def run_one(self, item: Row, model: str, client: _Recorder) -> Row:
        res = client.complete(**self._kwargs(item, model))
        roster = self._roster(item)
        raw = self.ex.parse_authors(res.parsed)
        aligned, anomalies = self.ex.align_to_roster(raw, roster)
        payloads = self.ex.resolve(aligned, self.canon)
        window = item["input"]["window"]
        authors, voided_n, quoted_n = [], 0, 0
        for p in payloads:
            _, envelope, voided = self.ex.anchor_payload(p, self.anchorer, window, item["input_digest"])
            voided_n += len(voided)
            quoted_n += len(envelope) + len(voided)
            affs = [{"surface": a["surface"], "org_slugs": list(a.get("org_slugs") or [])}
                    for a in p["affiliations"]]
            authors.append({
                "name": p["author_surface"], "author": p["author"], "affiliations": affs,
                "orgs": [a["surface"] for a in affs],
                "roster_ids": list(dict.fromkeys(s for a in affs for s in a["org_slugs"])),
                "resolution": p.get("resolution"), "email": p.get("email"),
                "voided": [v["quote"] for v in voided]})
        return {"authors": authors, "returned_authors": len(raw),
                "roster_mismatch": anomalies.get("roster_mismatch"),
                "fuzzy_name_matches": anomalies.get("fuzzy_name_matches", 0),
                "discovery_failed": not roster and not raw,
                "anchor_quoted": quoted_n, "anchor_voided": voided_n,
                "production_uses_llm": item.get("production_uses_llm", item.get("production_asks"))}


#: The tasks whose plan pin ``--pin`` may override.
PIN_TASKS = ("screen", "technique_staged", "model_roles")


def make_adapter(task: str, config: StackConfig, plan: Plan | None, *, prompt: str | None, models: list[str],
                 tier2: bool = True, tier2_model: str | None = None,
                 pin_overrides: Mapping[str, Any] | None = None) -> Adapter:
    if pin_overrides and task not in PIN_TASKS:
        raise EvalError(f"--pin: only stack eval {', '.join(PIN_TASKS)} take plan-pin overrides (not {task})")
    if task == "triage":
        return TriageAdapter(config, plan, prompt, models)
    if task == "screen":
        return ScreenAdapter(config, plan, prompt, models, tier2=tier2, tier2_model=tier2_model,
                             pin_overrides=pin_overrides)
    if task == "affiliation":
        return AffiliationAdapter(config, prompt, models)
    if task == "technique_staged":
        from stack.evals.annotators import TechniqueStagedAdapter

        return TechniqueStagedAdapter(config, plan, prompt, models, pin_overrides=pin_overrides)
    if task in ANNOTATOR_TASKS:
        from stack.evals.annotators import AnnotatorAdapter

        return AnnotatorAdapter(task, config, prompt, models, pin_overrides=pin_overrides)
    raise EvalError(f"unknown eval task {task!r} (known: {', '.join(TASKS)})")


def check_inputs(adapter: Adapter, items: Sequence[Row]) -> None:
    """Refuse when production code no longer produces an item's frozen user message (an item frozen
    without a call, ``production_asks`` false and ``input_digest`` null: when production now calls)."""
    bad: list[str] = []
    check_user = adapter.checks_user_message()
    for it in items:
        bad.extend(f"{it['item_id']} ({p})" for p in adapter.frozen_problems(it))
        if not check_user:
            continue
        if it.get("input_digest") is None and it.get("production_asks") is False:
            if adapter.capture(it, adapter.models[0], tier2=False) is not None:
                bad.append(f"{it['item_id']} (frozen without a call, production now asks)")
            continue
        got = sha256_text(adapter.user_message(it))
        if got != it["input_digest"]:
            bad.append(f"{it['item_id']} (frozen {it['input_digest'][:12]}, production {got[:12]})")
    if bad:
        raise EvalError(f"{len(bad)} item(s) whose input_digest no longer matches what production code "
                        f"sends (a new set is needed): " + "; ".join(bad[:10]) + (" …" if len(bad) > 10 else ""))


# ── estimate ─────────────────────────────────────────────────────────────────────────────────


def _request_of(probe: LlmClient, kw: Mapping[str, Any]) -> tuple[LlmRequest, str, str] | None:
    """The first attempt's wire request for ``complete(**kw)`` as the client builds it, with its
    rendered digest and basis digest (for the cache check); None when the gate refuses."""
    llm = probe._llm
    rc = llm.roles[kw["role"]]
    if kw.get("model") is not None and kw["model"] != rc.model:
        rc = rc.model_copy(update={"model": kw["model"]})
    max_tokens = kw.get("max_tokens") or rc.max_tokens
    models = probe._gated_models(rc, kw["license_class"])
    if not models:
        return None
    m = models[0]
    r = render_call(kw["template"], kw["refs"], model=m, max_tokens=max_tokens, schema=kw["schema"],
                    temperature=rc.temperature, reasoning_effort=rc.reasoning_effort)
    from stack.llm import _conversation, conversation_input

    prior = kw.get("prior") or ()
    idig = hashes.input_digest(conversation_input(prior, kw["user"]))
    req = probe._build_request(model=m, sent_prefix=r.sent_prefix,
                               messages=_conversation(prior, kw["user"]), schema=kw["schema"],
                               max_tokens=max_tokens, temperature=rc.temperature,
                               reasoning_effort=rc.reasoning_effort,
                               rule=probe._egress_rule(kw["license_class"]), attempt=1,
                               rendered_hash=r.rendered_hash, input_digest=idig)
    from stack.llm import rendered_digest

    return req, rendered_digest(r.rendered_hash, idig), kw.get("basis_digest") or idig


def estimate(adapter: Adapter, items: Sequence[Row], models: Sequence[str], repeat: int, *,
             probe: LlmClient, cache_dir: Callable[[str, int], Path]) -> Row:
    """The reservation and expected cost of the planned calls (module docstring)."""
    llm = probe._llm
    out_tokens = EXPECTED_OUT_TOKENS[adapter.task]
    per_model: dict[str, Row] = {}
    reserve_total = expected_total = tier2_all = 0.0
    for m in models:
        price = llm.price_for(m)
        pm = {"calls": 0, "cached": 0, "egress_skipped": 0, "reserve_usd": 0.0, "expected_usd": 0.0,
              "reserve_per_call_usd": 0.0, "tier2_reserve_all_usd": 0.0}
        caches = [ResponseCache.for_store(cache_dir(m, k)) for k in range(1, repeat + 1)]
        reserves: list[float] = []
        for it in items:
            kw = adapter.capture(it, m, tier2=False)
            if kw is None:  # production answers without a call (model_roles' empty menu): $0
                pm["no_call"] = pm.get("no_call", 0) + repeat
                continue
            built = _request_of(probe, kw)
            if built is None:
                pm["egress_skipped"] += repeat
                continue
            req, rdig, basis = built
            tin = reserve_tokens_in(req)
            res_usd = price.cost(tin, wire_max_tokens(req.payload))
            exp_usd = price.cost(tin, out_tokens)
            reserves.append(res_usd)
            for c in caches:
                pm["calls"] += 1
                if c.get(cache_key(req.model, rdig, basis)) is not None:
                    pm["cached"] += 1
                    continue
                pm["reserve_usd"] += res_usd
                pm["expected_usd"] += exp_usd
            kw2 = adapter.capture(it, m, tier2=True)
            if kw2 is not None:
                b2 = _request_of(probe, kw2)
                if b2 is not None:
                    p2 = llm.price_for(b2[0].model)
                    t2 = reserve_tokens_in(b2[0])
                    r2 = p2.cost(t2, wire_max_tokens(b2[0].payload)) * repeat
                    pm["tier2_reserve_all_usd"] += r2
                    pm["reserve_usd"] += r2 * TIER2_SHARE_ASSUMED
                    pm["expected_usd"] += p2.cost(t2, out_tokens) * repeat * TIER2_SHARE_ASSUMED
        pm["reserve_per_call_usd"] = sum(reserves) / len(reserves) if reserves else 0.0
        per_model[m] = pm
        reserve_total += pm["reserve_usd"]
        expected_total += pm["expected_usd"]
        tier2_all += pm["tier2_reserve_all_usd"]
    est: Row = {"items": len(items), "models": list(models), "repeats": repeat,
                "planned_calls": len(items) * len(models) * repeat,
                "expected_out_tokens": out_tokens, "per_model": per_model,
                "reserve_usd": reserve_total, "expected_usd": expected_total}
    if adapter.task == "screen":
        est["tier2_share_assumed"] = TIER2_SHARE_ASSUMED if getattr(adapter, "tier2", False) else 0.0
        est["tier2_reserve_all_usd"] = tier2_all
    return est


def format_estimate(est: Row, max_usd: float) -> str:
    lines = [(f"estimate: {est['items']} items x {len(est['models'])} models x {est['repeats']} repeats "
              f"= {est['planned_calls']} calls; reservations ${est['reserve_usd']:.4f} "
              f"(--max-usd ${max_usd:.2f}), expected ${est['expected_usd']:.4f} "
              f"(output ~{est['expected_out_tokens']} tokens)")]
    if "tier2_share_assumed" in est:
        lines.append(f"  screen tier 2 counted at an assumed {est['tier2_share_assumed']:.0%} escalation "
                     f"share; if every item escalated: +${est['tier2_reserve_all_usd']:.4f} reserved")
    for m, pm in est["per_model"].items():
        lines.append(f"  {m:<36} calls {pm['calls']:4d}  cached {pm['cached']:4d}  egress-skipped "
                     f"{pm['egress_skipped']:3d}  reserve/call ${pm['reserve_per_call_usd']:.5f}  "
                     f"reserve ${pm['reserve_usd']:.4f}  expected ${pm['expected_usd']:.4f}")
    return "\n".join(lines)


# ── the run ──────────────────────────────────────────────────────────────────────────────────


@dataclass
class EvalOptions:
    task: str
    set_id: str
    split: str
    models: list[str]
    repeat: int = 1
    max_usd: float = DEFAULT_MAX_USD
    prompt: str | None = None
    workers: int = DEFAULT_WORKERS
    estimate_only: bool = False
    items: list[str] | None = None
    limit: int | None = None
    baseline: str | None = None
    tier2: bool = True
    tier2_model: str | None = None
    gold: str | None = None
    pin_overrides: Mapping[str, Any] = field(default_factory=dict)
    evals_root: Path = DEFAULT_EVALS
    out_root: Path = DEFAULT_OUT
    overlay: Path = egress_overlay.DEFAULT_OVERLAY
    plan: Plan | None = None
    argv: Mapping[str, Any] = field(default_factory=dict)


def model_slug(model: str) -> str:
    return model.replace("/", "__").replace(":", "_")


def _utc() -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _git_head() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10, check=False,
                             cwd=Path(__file__).resolve().parent)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


#: The splits a set may carry: ``dev`` (prompt work), ``test`` (comparisons), ``holdout`` (a whole set
#: frozen for one pinned-config check, ``python -m stack.evals.freeze screen --split-all holdout``).
SPLITS = ("dev", "test", "holdout")


def _validate(opts: EvalOptions, llm: LlmConfig) -> None:
    if opts.task not in TASKS:
        raise EvalError(f"unknown eval task {opts.task!r} (known: {', '.join(TASKS)})")
    if opts.split not in SPLITS and not (opts.split == ALL_SPLIT and opts.task in ANNOTATOR_TASKS):
        extra = f" ({ALL_SPLIT}: {', '.join(ANNOTATOR_TASKS)} only)"
        raise EvalError(f"--split {opts.split!r}: one of {', '.join(SPLITS)}{extra}")
    if not opts.models or len(set(opts.models)) != len(opts.models):
        raise EvalError("--models: one or more distinct model ids")
    unpriced = [m for m in opts.models if m not in llm.pricing.models]
    if unpriced:
        raise EvalError(f"models {unpriced} have no llm.yaml pricing entry (D173: price a model before "
                        f"any call; never guess an id)")
    retired = [m for m in opts.models if llm.is_retired(m)]
    if retired:
        raise EvalError(f"models {retired} are retired in llm.yaml (their price only re-prices old runs)")
    if opts.repeat < 1:
        raise EvalError("--repeat must be >= 1")
    if not 1 <= opts.workers <= MAX_WORKERS:
        raise EvalError(f"--workers must be 1..{MAX_WORKERS}")
    if opts.max_usd <= 0:
        raise EvalError("--max-usd must be > 0")
    if opts.baseline is not None and opts.baseline not in opts.models:
        raise EvalError(f"--baseline {opts.baseline!r} is not one of --models")


def _stderr_warning(message: str) -> None:
    sys.stderr.write(f"warning: {message}\n")


def run_eval(opts: EvalOptions, *, config: StackConfig | None = None, echo: Callable[[str], None] = print,
             warn: Callable[[str], None] = _stderr_warning) -> Path:
    """Run (or only estimate) one eval; returns the run directory. ``warn`` gets the adapter's
    provenance warnings (:meth:`Adapter.provenance`) before the estimate."""
    from stack.annotate.stage import stack_config
    from stack.evals import score

    base = config if config is not None else stack_config()
    _validate(opts, base.llm)
    try:
        ov = egress_overlay.load_overlay(opts.overlay)
        cfg = egress_overlay.overlaid_config(base, ov)
    except egress_overlay.OverlayError as exc:
        raise EvalError(str(exc)) from exc
    llm = cfg.llm
    eset = load_set(opts.evals_root, opts.task, opts.set_id)
    items = select_items(eset.items, opts.split, opts.items, opts.limit)
    adapter = make_adapter(opts.task, cfg, opts.plan, prompt=opts.prompt, models=opts.models,
                           tier2=opts.tier2, tier2_model=opts.tier2_model, pin_overrides=opts.pin_overrides)
    adapter.set_dir = eset.dir
    set_root = opts.out_root / opts.task / opts.set_id
    adapter.cache_root = set_root / "cache"
    provenance = adapter.provenance()
    for w in provenance.get("warnings") or []:
        warn(w)
    gold_file = opts.gold or score.default_gold(eset.dir)
    if opts.gold is not None and not score.gold_path(eset.dir, opts.gold).is_file():
        raise EvalError(f"--gold {opts.gold!r}: no file {score.gold_path(eset.dir, opts.gold)}")
    absent = adapter.gold_problems(score.load_gold(eset.dir, gold_file)[1])
    if absent:
        raise EvalError(f"{len(absent)} gold id(s) of {gold_file} are not in the current menu source (a renamed or "
                        f"removed slug or node; fix the gold or re-freeze the set): " + ", ".join(absent[:20])
                        + (" …" if len(absent) > 20 else ""))

    config_key = {"task": opts.task, "set": opts.set_id, "split": opts.split, "models": opts.models,
                  "repeat": opts.repeat, "prompt": adapter.prompt_name, "items": [i["item_id"] for i in items],
                  "tier2": opts.tier2, "tier2_model": getattr(adapter, "tier2_model", None)}
    if opts.pin_overrides:  # absent otherwise, so earlier runs' directory hashes are unchanged
        config_key["pin_overrides"] = dict(opts.pin_overrides)
    short = hashlib.sha256(hashes.jcs(config_key).encode("utf-8")).hexdigest()[:8]
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    run_dir = set_root / f"{stamp}_{opts.split}_{short}"
    n = 1
    while run_dir.exists():  # the same command twice within a second
        n += 1
        run_dir = set_root / f"{stamp}_{opts.split}_{short}-{n}"
    run_id = f"eval-{opts.task}-{run_dir.name}"
    adapter.run_id = run_id
    stage = f"eval.{opts.task}"

    def cache_dir(model: str, k: int) -> Path:
        return set_root / "cache" / model_slug(model) / f"r{k}"

    check_inputs(adapter, items)  # before any call

    probe = LlmClient(config=llm, transport=_NoSend(), ledger=Ledger(), run_id=run_id + "-estimate",
                      stage=stage, cache=None)
    est = adapter.estimate(items, opts.models, opts.repeat, probe=probe, cache_dir=cache_dir)
    echo(adapter.format_estimate(est, opts.max_usd))
    over = est["reserve_usd"] - opts.max_usd > 1e-9
    run: Row = {
        "run_id": run_id, "task": opts.task, "set": opts.set_id, "set_dir": str(eset.dir.resolve()),
        "split": opts.split, "models": opts.models, "repeat": opts.repeat, "argv": dict(opts.argv),
        "prompt": adapter.prompt_name, "pins": adapter.pins(), "git_head": _git_head(), "gold": gold_file,
        "pin_overrides": dict(opts.pin_overrides),
        "input_check": "user_message+frozen" if adapter.checks_user_message() else "frozen_only",
        "set_manifest_digest": eset.manifest_digest, "set_items_digest": eset.items_digest,
        "items": [i["item_id"] for i in items], "max_usd": opts.max_usd, "workers": opts.workers,
        "baseline": opts.baseline or opts.models[0],
        "egress_overlay": {"path": str(opts.overlay), "digest": egress_overlay.overlay_digest(opts.overlay),
                           "content": ov},
        "cache_root": str((set_root / "cache").resolve()), "estimate": est, **provenance,
    }
    if opts.estimate_only:
        run_dir.mkdir(parents=True, exist_ok=False)
        run.update(status="estimate", started=_utc(), ended=_utc(), refused_by_estimate=over)
        (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", "utf-8")
        echo(f"estimate only (no calls): {run_dir}" + ("; a run would be REFUSED: raise --max-usd" if over else ""))
        return run_dir
    if over:
        raise EvalError(f"estimate: reservations ${est['reserve_usd']:.4f} > --max-usd ${opts.max_usd:.2f}; "
                        f"the run does not start (raise --max-usd)")

    # Clients first (a missing key fails here, before any reservation).
    clients: dict[tuple[str, int], LlmClient] = {}
    for m in opts.models:
        for k in range(1, opts.repeat + 1):
            clients[(m, k)] = adapter.open_client(cfg, run_id=run_id, stage=stage, model=m, repeat=k,
                                                  store_root=cache_dir(m, k), budget_usd=opts.max_usd)
    run_dir.mkdir(parents=True, exist_ok=False)
    run["started"] = _utc()

    allowed = {(m, it["item_id"]): bool(probe._gated_models(adapter.tier1_role(m), license_of(it)))
               for m in opts.models for it in items}
    scope = BudgetScope()
    halted = threading.Event()
    progress = _Progress(len(items) * len(opts.models) * opts.repeat, echo)

    def one(k: int, it: Row, m: str) -> Row:
        row = _base_row(it, m, k)
        try:
            if not allowed[(m, it["item_id"])]:
                return {**row, "status": "skipped", "error_class": "egress"}
            if halted.is_set():
                return {**row, "status": "not_attempted", "error_class": "halted"}
            rec = adapter.recorder(clients[(m, k)])
            try:
                out = scope.call(functools.partial(adapter.run_one, it, m, rec))
            except EgressRefused as exc:
                return {**row, **adapter.accounting(rec), "status": "skipped", "error_class": "egress",
                        "error": str(exc)[:500]}
            except LlmError as exc:
                if isinstance(exc, FatalLlmError):
                    halted.set()
                return {**row, **adapter.accounting(rec), "status": exc.status, "error_class": exc.error_class,
                        "error": str(exc)[:500]}
            if out is None:
                return {**row, **adapter.accounting(rec), "status": "not_attempted", "error_class": "budget"}
            return {**row, **adapter.accounting(rec), **out, "status": "ok", "error_class": None}
        finally:
            progress.tick()

    order = [(k, it, m) for k in range(1, opts.repeat + 1) for it in items for m in opts.models]
    rows: list[Row] = []
    error: Exception | None = None  # a row's non-LlmError (a code bug): the other rows are still written
    try:
        with ThreadPoolExecutor(max_workers=opts.workers) as pool:
            for f in [pool.submit(one, *t) for t in order]:
                try:
                    rows.append(f.result())
                except Exception as exc:  # noqa: BLE001 - re-raised below, after rows/run.json/spend
                    error = error or exc
    finally:
        spend_now = close_run(run_id)
        forget_run(run_id)
    model_pos = {m: i for i, m in enumerate(opts.models)}
    rows.sort(key=lambda r: (model_pos[r["model"]], r["repeat"], r["item_id"]))
    write_jsonl(run_dir / "rows.jsonl", rows)

    settled = {m: sum(float(r.get("cost_usd") or 0.0) for r in rows if r["model"] == m) for m in opts.models}
    run.update(
        ended=_utc(), spend=spend_now,
        status="failed" if error is not None else "halted" if halted.is_set() else "ok",
        budget_tripped=scope.tripped, **({"error": repr(error)[:2000]} if error is not None else {}),
        settled_usd=sum(settled.values()), settled_usd_by_model=settled,
        counts=dict(Counter(f"{r['status']}[{r['error_class']}]" if r["error_class"] else r["status"]
                            for r in rows)),
    )
    (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", "utf-8")
    opts.out_root.mkdir(parents=True, exist_ok=True)
    with (opts.out_root / SPEND_FILE).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"run_id": run_id, "run_dir": str(run_dir), "task": opts.task, "set": opts.set_id,
                             "split": opts.split, "models": opts.models, "started": run["started"],
                             "ended": run["ended"], "settled_usd": run["settled_usd"],
                             "settled_usd_by_model": settled}, sort_keys=True) + "\n")
    if error is not None:  # rows.jsonl holds the rows that finished; no scoring (review L5 LOW, D432)
        raise error
    summary = score.score_run(run_dir, baseline=opts.baseline)
    echo(summary["markdown"])
    echo(f"run: {run_dir} ({run['status']}; settled ${run['settled_usd']:.6f}"
         + ("; budget tripped: later rows not_attempted[budget]" if scope.tripped else "") + ")")
    return run_dir


def _base_row(it: Row, model: str, k: int) -> Row:
    return {"item_id": it["item_id"], "doc_id": it["doc_id"], "split": it["split"],
            "stratum": it.get("stratum"), "hard": it.get("hard"), "license_class": it.get("license_class"),
            "production_asks": it.get("production_asks"), "model": model, "repeat": k,
            "status": None, "error_class": None, "error": None, "verdict": None, "confidence": None,
            "reasoning": None, "answer_model": None, "cost_usd": 0.0, "reserved_usd": 0.0,
            "tokens_in": 0, "tokens_out": 0, "latency_ms": 0, "cache_hit": False,
            "schema_degraded": False, "unwrapped": False, "attempts": 0, "attempt_errors": []}


def _accounting(rec: _Recorder) -> Row:
    """Settled cost, reservations, tokens, latency and attempts over every call of the row; and
    the provider-cache usage fields (:func:`stack.evals.score.row_usage`) read from each answered
    call's response-cache entry, which holds the provider's response body (``LlmResult`` carries
    no ``usage``); a replayed call's entry is the original call's."""
    from stack.evals.score import row_usage

    results = [c for c in rec.calls if isinstance(c, LlmResult)]
    attempts = [a for c in rec.calls for a in (c.attempts if isinstance(c, LlmResult)
                                               else getattr(c, "attempts", ()) or ())]
    cost = sum(r.cost_usd for r in results) + sum(float(getattr(c, "cost_usd", 0.0) or 0.0)
                                                  for c in rec.calls if not isinstance(c, LlmResult))
    cache = getattr(rec.client, "cache", None)
    entries = [(cache.get(cache_key(r.model, r.rendered_digest, r.basis_digest)) if cache is not None else None,
                r.is_cache_hit) for r in results]
    return {
        **row_usage(entries, cost, rec.client._llm.price_for),
        "answer_model": results[0].model if results else None,
        "cost_usd": cost, "reserved_usd": sum(a.reserved_usd for a in attempts),
        "tokens_in": sum(r.tokens_in for r in results), "tokens_out": sum(r.tokens_out for r in results),
        "latency_ms": sum(a.latency_ms for a in attempts),
        "cache_hit": bool(results) and len(results) == len(rec.calls) and all(r.is_cache_hit for r in results),
        "schema_degraded": any(r.degraded for r in results), "unwrapped": any(r.unwrapped for r in results),
        "attempts": len(attempts), "attempt_errors": [a.error_class for a in attempts if a.error_class],
    }


class _Progress:
    """A stderr-free progress line every 10 % (through ``echo``)."""

    def __init__(self, total: int, echo: Callable[[str], None]) -> None:
        self.total, self.echo, self.done = total, echo, 0
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._step = max(1, total // 10)

    def tick(self) -> None:
        with self._lock:
            self.done += 1
            if self.done % self._step == 0 or self.done == self.total:
                self.echo(f"  {self.done}/{self.total} rows ({time.monotonic() - self._t0:.0f} s)")


# ── prompt-structure audit ───────────────────────────────────────────────────────────────────

#: (task, prompt pin name) pairs the audit renders: both triage prompts, both screen templates
#: (``screen-v7`` = ``screen.md``), the affiliation workflow's one prompt.
AUDIT_PROMPTS: tuple[tuple[str, str | None], ...] = (
    ("triage", "safety-relevance-v1"), ("triage", "safety-relevance-v2"),
    ("screen", "screen-v7"), ("screen", "screen-v8"), ("affiliation", None))
AUDIT_MODEL = "anthropic/claude-haiku-5.5"
_CACHE_MARKER = re.compile(r"^\s*(cache|cache_control|prompt_cache\w*)\s*:", re.MULTILINE)


def _cache_control_paths(node: Any, path: str = "") -> list[str]:
    if isinstance(node, Mapping):
        out = [f"{path}.cache_control" if path else "cache_control"] if "cache_control" in node else []
        for k, v in node.items():
            out += _cache_control_paths(v, f"{path}.{k}" if path else str(k))
        return out
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _cache_control_paths(v, f"{path}[{i}]")]
    return []


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(str(b.get("text", "")) for b in content if isinstance(b, Mapping))


def config_cache_markers(configs_dir: Path) -> list[str]:
    """``file:line`` of every ``cache:`` / ``cache_control:`` / ``prompt_cache…:`` key under
    ``configs_dir`` (YAML)."""
    out = []
    for p in sorted([*configs_dir.rglob("*.yaml"), *configs_dir.rglob("*.yml")]):
        text = p.read_text("utf-8")
        out += [f"{p}:{text.count(chr(10), 0, m.start()) + 1}" for m in _CACHE_MARKER.finditer(text)]
    return out


def audit_prompts(*, set_id: str, model: str = AUDIT_MODEL, evals_root: Path = DEFAULT_EVALS,
                  out_root: Path | None = DEFAULT_OUT, config: StackConfig | None = None, plan: Plan | None = None,
                  overlay: Path = egress_overlay.DEFAULT_OVERLAY, configs_dir: Path = Path("configs")) -> Row:
    """Render one real frozen item's request per (task, prompt) of :data:`AUDIT_PROMPTS` through
    the production client path (the task adapter's captured ``complete`` kwargs →
    ``render_call`` → ``LlmClient._build_request``; a transport that refuses to send) and report
    the request's structure: the fixed system prefix (chars, ≈ tokens at chars/4), the per-document
    user message, message order, where ``cache_control`` sits, whether the prefix is byte-identical
    for a second item, where the document sits in the user message (chars before / after it), and
    the response-format schema size (folded into a user turn after the document when the client
    degrades the strict schema). Screen also renders its tier-2 request. The first two ``dev``
    items (``item_id`` order, items production asks first: a title-only triage probe is not
    representative) the egress gate lets ``model`` see are used. Also lists any cache
    marker key in ``configs/``. Writes ``<out>/audit-prompts-<stamp>.md`` unless ``out_root`` is
    None."""
    from stack.annotate.stage import stack_config
    from workflows.annotate.screen import main as screen_main

    base = config if config is not None else stack_config()
    try:
        cfg = egress_overlay.overlaid_config(base, egress_overlay.load_overlay(overlay))
    except egress_overlay.OverlayError as exc:
        raise EvalError(str(exc)) from exc
    if model not in cfg.llm.pricing.models:
        raise EvalError(f"--model {model!r} has no llm.yaml pricing entry")
    if cfg.llm.is_retired(model):
        raise EvalError(f"--model {model!r} is retired in llm.yaml")
    probe = LlmClient(config=cfg.llm, transport=_NoSend(), ledger=Ledger(), run_id="eval-audit-prompts",
                      stage="eval.audit", cache=None)
    saved_template = screen_main.TEMPLATE
    entries: list[Row] = []
    try:
        for task, prompt in AUDIT_PROMPTS:
            eset = load_set(evals_root, task, set_id)
            adapter = make_adapter(task, cfg, plan, prompt=prompt, models=[model], tier2=True)
            adapter.set_dir = eset.dir
            for tier2 in ((False, True) if task == "screen" else (False,)):
                built: list[tuple[Row, LlmRequest]] = []
                dev = (i for i in eset.items if i["split"] == "dev")
                for it in sorted(dev, key=lambda i: (i.get("production_asks") is False, i["item_id"])):
                    kw = adapter.capture(it, model, tier2=tier2)
                    req = _request_of(probe, kw) if kw is not None else None
                    if req is not None:
                        built.append((it, req[0]))
                    if len(built) == 2:
                        break
                if not built:
                    raise EvalError(f"audit: no dev item of {task}/{set_id} the egress gate lets {model} see")
                entries.append(_audit_entry(task, adapter.prompt_name, tier2, built, adapter))
    finally:
        screen_main.TEMPLATE = saved_template
    markers = config_cache_markers(configs_dir)
    md = _audit_markdown(set_id, model, entries, markers, configs_dir)
    path = None
    if out_root is not None:
        out_root.mkdir(parents=True, exist_ok=True)
        path = out_root / f"audit-prompts-{dt.datetime.now(dt.UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
        path.write_text(md + "\n", "utf-8")
    return {"entries": entries, "config_cache_markers": markers, "markdown": md, "path": str(path) if path else None}


def _audit_document(task: str, item: Row, tier2: bool, adapter: Adapter) -> str:
    inp = item["input"]
    if task == "triage":
        return str(inp["record"])
    if task == "screen":
        return adapter.full_text(item) if tier2 else str(inp["text"])
    return str(inp["window"])


def _audit_entry(task: str, prompt: str, tier2: bool, built: list[tuple[Row, LlmRequest]], adapter: Adapter) -> Row:
    from stack.llm import estimate_tokens

    item, req = built[0]
    msgs = list(req.payload["messages"])
    system = _text_of(msgs[0]["content"]) if msgs and msgs[0]["role"] == "system" else ""
    users = [m for m in msgs if m["role"] == "user"]
    user = _text_of(users[-1]["content"]) if users else ""
    doc = _audit_document(task, item, tier2, adapter)
    at = user.find(doc)
    schema = ((req.payload.get("response_format") or {}).get("json_schema") or {}).get("schema")
    return {
        "task": task, "prompt": prompt, "tier": 2 if tier2 else 1, "model": req.model, "item_id": item["item_id"],
        "system_chars": len(system), "system_tokens_est": estimate_tokens(system),
        "user_chars": len(user), "user_tokens_est": estimate_tokens(user),
        "roles": [m["role"] for m in msgs], "cache_control": _cache_control_paths(req.payload),
        "prefix_identical_2nd_item": (_text_of(built[1][1].payload["messages"][0]["content"]) == system
                                      if len(built) > 1 else None),
        "second_item_id": built[1][0]["item_id"] if len(built) > 1 else None,
        "doc_found": at >= 0, "chars_before_doc": at if at >= 0 else None,
        "chars_after_doc": len(user) - at - len(doc) if at >= 0 else None,
        "schema_chars": len(hashes.jcs(schema)) if schema is not None else 0,
    }


def _audit_markdown(set_id: str, model: str, entries: Sequence[Row], markers: Sequence[str], configs_dir: Path) -> str:
    lines = [f"# stack eval audit-prompts: set {set_id}, model {model}", "",
             ("One real frozen dev item per row, rendered by the production client path (no send). Tokens ≈ chars/4 "
              "(the measured `prompt_tokens` per row are in each run's summary). *doc* = the frozen document text "
              "(triage the record, screen tier 1 the truncated text / tier 2 the whole text, affiliation the byline "
              "window) located in the last user message."), "",
             ("| task | prompt | tier | item | system prefix chars (≈tok) | user chars (≈tok) | message order | "
              "cache_control at | prefix same on 2nd item | user: chars before doc / after doc | schema chars |"),
             "|---|---|---|---|---|---|---|---|---|---|---|"]
    for e in entries:
        doc = (f"{e['chars_before_doc']} / {e['chars_after_doc']}" if e["doc_found"] else "doc not found verbatim")
        same = "n/a" if e["prefix_identical_2nd_item"] is None else (
            f"{'yes' if e['prefix_identical_2nd_item'] else 'NO'} ({e['second_item_id']})")
        lines.append(f"| {e['task']} | {e['prompt']} | {e['tier']} | {e['item_id']} | {e['system_chars']} "
                     f"({e['system_tokens_est']}) | {e['user_chars']} ({e['user_tokens_est']}) | "
                     f"{' → '.join(e['roles'])} | {', '.join(e['cache_control']) or 'none'} | {same} | {doc} | "
                     f"{e['schema_chars']} |")
    lines += ["", f"Cache marker keys in `{configs_dir}/`: " + (", ".join(markers) if markers else "none") + "."]
    return "\n".join(lines)


# ── spend ────────────────────────────────────────────────────────────────────────────────────


def spend(out_root: Path = DEFAULT_OUT) -> Row:
    """Track E's settled spend: the sum over ``<out>/spend.jsonl`` (one line per finished run)."""
    path = out_root / SPEND_FILE
    runs = read_jsonl(path) if path.is_file() else []
    by_task: defaultdict[str, float] = defaultdict(float)
    by_model: defaultdict[str, float] = defaultdict(float)
    for r in runs:
        by_task[r["task"]] += float(r["settled_usd"])
        for m, v in (r.get("settled_usd_by_model") or {}).items():
            by_model[m] += float(v)
    return {"runs": len(runs), "settled_usd": sum(by_task.values()), "by_task": dict(by_task),
            "by_model": dict(by_model)}


__all__ = [
    "ALL_SPLIT",
    "ANNOTATOR_TASKS",
    "AUDIT_PROMPTS",
    "DEFAULT_EVALS",
    "DEFAULT_MAX_USD",
    "DEFAULT_OUT",
    "REPO_ROOT",
    "TASKS",
    "TIER2_SHARE_ASSUMED",
    "EvalError",
    "EvalOptions",
    "audit_prompts",
    "check_inputs",
    "estimate",
    "license_of",
    "load_set",
    "make_adapter",
    "run_eval",
    "spend",
]
