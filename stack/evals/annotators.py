"""``stack eval model_roles | technique_roles | technique_staged | model_mentions`` (track AE; technique_staged:
techpipe D607, :class:`TechniqueStagedAdapter`, set ``evals/technique_staged/2026-10-17a``): the annotators on a frozen
annotator set (``evals/<task>/<set>/``). Sets: ``2026-10-13a`` (frozen by
``evals/model_roles/2026-10-13a/scripts/freeze.py``, the same ``item_id``s in both task directories;
technique_roles' set, model_roles' historical one; technique_roles' half is rebuilt at HEAD, byte for byte,
by ``evals/technique_roles/2026-10-13a/scripts/freeze.py``); for model_roles ``2026-10-17a`` (current: registry
version 3, prompt ``model-roles-v2``, 37 items including no-model, off-catalogue and concretization items;
``evals/model_roles/2026-10-17a/README.md``) and ``2026-10-16a`` (historical: the merge, D591).

**model_roles, technique_roles** (:class:`AnnotatorAdapter`, run by :func:`stack.evals.harness.run_eval`)
call production ``main.ask`` (``workflows/annotate/<task>/main.py``) as the screen adapter calls screen's:
an ``AskItem`` whose text is the frozen whole text (``texts/<item_id>.txt.gz``, checked against
``input.full_text_digest``), ``basis_kind`` ``text`` (both, the registry's), ``basis_digest`` =
``input.text_digest``, the item's ``license_class`` (NULL: ``restricted``, as production;
:func:`stack.evals.harness.license_of`), ``row = {doc_id}``; an ``AskContext`` with ``plan_pin =
{name, model}`` (role: production's default inside ``pin_of``, ``extract_main``) and ``pin =
main.prompt(plan_pin, llm)``. ``--prompt`` is the pin name (it selects the prompt file,
``prompts/<name>.md``, as in production; D591). So the scan and the menu (model_roles, D591: the
matcher over the current ``configs/models.yaml``; the frozen ``input.model_mentions`` is not read), the
window (``DOC_CHAR_CAP``), ``MAX_TOKENS`` (16,000), the schema, the parse/repair (``judge.normalize``)
and the quote verification are production's.

The input check (:func:`stack.evals.harness.check_inputs`) recomputes the user message with a
capture probe that never sends and compares its sha256 with ``input_digest``. For model_roles the user
message depends on the prompt file, so a set that records its prompt (manifest
``frozen_with.model_roles_prompt``, sets 2026-10-16a and 2026-10-17a) compares ``input_digest`` only when every pin names
that prompt, and checks the prompt-independent part for any prompt (:meth:`AnnotatorAdapter.frozen_problems`):
the per-document fills (``main.fills``: the document window, the truncation note, the menu) must hash to
``input.fills_digest`` (:func:`fills_digest`); since model_roles registry version 3 every item is asked, so an
item frozen with ``production_asks: false`` is refused. So any ``--prompt`` runs on the set as long as the scan and the menus are the frozen ones. Since
model_roles scans with the current vocabulary, an item whose scan or menu text changed since the freeze
is refused until the set is re-frozen (``scripts/freeze.py``; re-frozen on vocabulary v0.7 on 2026-10-17).
``no_call`` is false on every model_roles row since version 3 (it stays for older runs' rows).

Row fields beyond the harness's standard ones: ``ann_status`` (the Answer's ``ok`` / ``empty``),
``payload`` (the stored payload the Answer carries), ``counts`` (the Answer's counts), ``no_call``.

**Menu source.** The judge's menu comes from a file outside the frozen texts: model_roles'
``configs/models.yaml``, technique_roles' taxonomy (``main.TAXONOMY_PATH``). A set records the one it
was frozen with in ``manifest.json`` ``frozen_with`` (``models_yaml_sha256`` / ``_version``;
``taxonomy_sha256`` / ``_version`` / ``_path``), and ``run.json`` records the current and the frozen
digest (``vocab_digest`` / ``frozen_vocab_digest``; ``taxonomy_digest`` / ``frozen_taxonomy_digest``).
Before any call :func:`stack.evals.harness.run_eval` refuses the run when a gold id (:func:`gold_ids`:
a slug, a node id) is not in the current file (:meth:`AnnotatorAdapter.gold_problems`, naming them):
the input check hashes the document's user message, which a renamed slug does not change, and the
scorer would count every gold row of the old name as a miss. When every gold id exists but the digest
differs, the run goes ahead with a warning (the menu text may have moved; for model_roles a moved menu
also fails the fills check). The
scorer's ``extras`` block (:func:`stack.evals.score.extras_block`) measures the off-catalogue extras on
items whose gold lists ``expected_extras``.

**model_mentions** (:func:`run_mentions`, no LLM, no ledger, no client): production ``match.scan_payload``
(``workflows/annotate/model_roles/match.py`` over the current ``configs/models.yaml``) over every frozen whole text of the model_roles set; each item
is compared with its frozen ``input.model_mentions.payload`` (a difference = a vocabulary or matcher
change since the freeze) and scored against gold ``label.mentions`` with
:func:`stack.evals.score.mentions_block`. Writes ``run.json``, ``rows.jsonl``, ``summary.json`` and
``summary.md`` under ``<out>/model_mentions/<set>/<stamp>_<split>/``.
"""

from __future__ import annotations

import datetime as dt
import importlib
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from stack.core.hashes import jcs
from stack.evals.harness import (
    ANNOTATOR_TASKS,
    DEFAULT_EVALS,
    DEFAULT_OUT,
    DEFAULT_PROMPT,
    Adapter,
    EvalError,
    Row,
    _CaptureClient,
    _Captured,
    _git_head,
    _Recorder,
    license_of,
    load_set,
    read_frozen_text,
    select_items,
    sha256_text,
    write_jsonl,
)

if TYPE_CHECKING:
    from stack.config import RoleConfig, StackConfig

#: The production workflow module of each annotator task.
WORKFLOW_MODULES = {"model_roles": "workflows.annotate.model_roles.main",
                    "technique_roles": "workflows.annotate.technique_roles.main",
                    "technique_staged": "workflows.annotate.technique_staged.main",
                    "model_mentions": "workflows.annotate.model_roles.match"}  # the matcher alone (D591)
#: The ``AskItem.basis_kind`` production gives each (the registry's basis).
BASIS_KIND = {"model_roles": "text", "technique_roles": "text", "technique_staged": "text"}
#: model_mentions reads the model_roles set (same items, same whole texts, the frozen scan payload).
MENTIONS_SET_TASK = "model_roles"


def workflow_module(task: str) -> Any:
    return importlib.import_module(WORKFLOW_MODULES[task])


class AnnotatorAdapter(Adapter):
    """model_roles / technique_roles through production ``main.ask`` (module docstring)."""

    #: Plan-pin keys ``--pin`` may not set (each has its own option); model_roles only takes ``--pin``.
    OWN_OPTION_KEYS = {"name": "--prompt", "model": "--models"}

    def __init__(self, task: str, config: StackConfig, prompt: str | None, models: list[str], *,
                 pin_overrides: Mapping[str, Any] | None = None) -> None:
        if task not in ANNOTATOR_TASKS:
            raise EvalError(f"not an annotator task: {task!r}")
        overrides = dict(pin_overrides or {})
        own = sorted(k for k in overrides if k in self.OWN_OPTION_KEYS)
        if own:
            raise EvalError(f"--pin {own}: set with {', '.join(self.OWN_OPTION_KEYS[k] for k in own)} instead")
        if overrides and task != "model_roles":
            raise EvalError(f"--pin: {task} takes no plan-pin overrides")
        name = prompt or DEFAULT_PROMPT[task]
        super().__init__(task, config, str(name), models)
        self.main = workflow_module(task)
        self.pin_overrides = overrides
        self.plan_pins: dict[str, dict[str, Any]] = {}
        self.prompt_pins: dict[str, Any] = {}
        for m in models:
            # --pin keys (model_roles: ``doc_cap``) are checked by the stage's pin_of / prompt
            p: dict[str, Any] = {"name": name, "model": m, **overrides}
            try:
                self.prompt_pins[m] = self.main.prompt(p, config.llm)
            except Exception as exc:  # StageError: a bad pin
                raise EvalError(f"{task}: {exc}") from exc
            self.plan_pins[m] = p

    def pins(self) -> dict[str, Any]:
        return {m: {**p, "role": self.main.pin_of(p, self.config.llm).role,
                    "template_digests": list(self.prompt_pins[m].template_digests),
                    "models": list(self.prompt_pins[m].models)} for m, p in self.plan_pins.items()}

    def tier1_role(self, model: str) -> RoleConfig:
        role = self.main.pin_of(self.plan_pins[model], self.config.llm).role
        return self.config.llm.roles[role].model_copy(update={"model": model})

    def provenance(self) -> Row:
        """The menu source the run uses and the one the set was frozen with (module docstring, "Menu
        source"): model_roles' vocabulary digests (``vocab_digest``, ``frozen_vocab_digest``),
        technique_roles' taxonomy digests (``taxonomy_digest``, ``frozen_taxonomy_digest``) and
        version; a ``warnings`` entry when the set records a digest and the current file differs (the
        gold ids are checked separately, :meth:`gold_problems`)."""
        cur = self.menu_source()
        frozen_with = _frozen_with(self.set_dir)
        key = MENU_SOURCE_KEYS[self.task]
        frozen = frozen_with.get(key[1])
        out: Row = ({"vocab_digest": cur["sha256"], "frozen_vocab_digest": None if frozen is None else str(frozen)}
                    if self.task == "model_roles" else
                    {"taxonomy_digest": cur["sha256"], "taxonomy_version": cur["version"],
                     "frozen_taxonomy_digest": None if frozen is None else str(frozen)})
        if frozen is not None and frozen != cur["sha256"]:
            out["warnings"] = [
                f"{self.task}: {key[0]} is not the one set {self.set_dir.name if self.set_dir else '?'} was "
                f"frozen with (sha256 {cur['sha256'][:12]} version {cur['version']}, frozen "
                f"{str(frozen)[:12]} version {frozen_with.get(key[2])}): a gold id no longer in it refuses the "
                f"run; otherwise the menu text the judge sees may differ from the frozen one"]
        return out

    def menu_source(self) -> dict[str, str]:
        """``{sha256, version}`` of the current menu source: ``configs/models.yaml`` (model_roles) or
        the taxonomy file (technique_roles, ``main.TAXONOMY_PATH``)."""
        if self.task == "model_roles":
            vocab = self.main.match.scanner_for(self.config.config_dir).vocab
            return {"sha256": str(vocab.digest), "version": str(vocab.validated.version)}
        m = self.main.menu()
        return {"sha256": str(m.taxonomy_sha256), "version": str(m.taxonomy_version)}

    def menu_ids(self) -> frozenset[str]:
        """The ids gold may name: the vocabulary's node ids (model_roles) or the menu's slugs."""
        if self.task == "model_roles":
            return frozenset(self.main.match.scanner_for(self.config.config_dir).vocab.nodes)
        return frozenset(self.main.menu().slugs)

    def gold_problems(self, gold: Mapping[str, Row]) -> list[str]:
        """Gold ids (:func:`gold_ids`) absent from the current menu source, ``<item_id>:<id>`` (module
        docstring, "Menu source"): a renamed or removed slug or node would silently turn its gold rows
        into misses, so the run is refused before any call."""
        ids = self.menu_ids()
        return [f"{iid}:{x}" for iid in sorted(gold) for x in gold_ids(self.task, gold[iid]) if x not in ids]

    def frozen_problems(self, item: Row) -> list[str]:
        """The whole text against ``input.full_text_digest``; for a model_roles item with
        ``input.fills_digest`` (module docstring) also the scan's ask decision and the fills."""
        text = self.full_text(item)
        got = sha256_text(text)
        if got != item["input"]["full_text_digest"]:
            return [f"whole text (texts/{item['item_id']}.txt.gz) hashes to {got[:12]}, frozen "
                    f"full_text_digest {item['input']['full_text_digest'][:12]}"]
        if self.task != "model_roles" or "fills_digest" not in item["input"]:
            return []
        if not item["production_asks"]:  # frozen under registry version 2; production judges every item now
            return ["frozen production_asks false, production asks every document since model_roles version 3"]
        c = self.main.match.scanner_for(self.config.config_dir)
        now = fills_digest(self.main.fills(text, c.scanner.scan(text), c.vocab)[2])
        frozen = str(item["input"]["fills_digest"])
        return [] if now == frozen else [f"fills (window, menu) hash to {now[:12]}, frozen fills_digest {frozen[:12]}"]

    def checks_user_message(self) -> bool:
        """model_roles on a set that records its prompt: only when every pin names that prompt (the
        frozen ``input_digest`` is that prompt's user message) and no ``--pin`` override changes what it sends
        (the digest was frozen under the default pin; the text and fills checks still run); otherwise always."""
        if self.task != "model_roles":
            return True
        if self.pin_overrides:
            return False
        frozen = _frozen_with(self.set_dir).get("model_roles_prompt")
        return frozen is None or all(p["name"] == frozen for p in self.plan_pins.values())

    def _ask_args(self, item: Row, model: str, client: Any) -> tuple[Any, Any]:
        from stack.annotate.stage import AskContext, AskItem

        inp = item["input"]
        text = self.full_text(item)
        row: dict[str, Any] = {"doc_id": item["doc_id"]}
        ask_item = AskItem(doc_id=item["doc_id"], source_id=item["doc_id"], basis_kind=BASIS_KIND[self.task],
                           basis_digest=str(inp["text_digest"]), fields=None, row=row,
                           license_class=license_of(item), attempt=1, load_text=lambda: text)
        cfg = self.config
        actx = AskContext(run_id=self.run_id, stage=f"eval.{self.task}", store_root=None,
                          pin=self.prompt_pins[model], plan_pin=self.plan_pins[model], options={},
                          budget_usd=None, _config=lambda: cfg, _client=client)
        return ask_item, actx

    def capture(self, item: Row, model: str, *, tier2: bool) -> dict[str, Any] | None:
        """The ``complete`` kwargs of the item's one call; None when production answers without one."""
        if tier2:
            return None
        probe = _CaptureClient(1)
        try:
            self.main.ask(*self._ask_args(item, model, probe))
        except _Captured:
            return probe.captured[-1]
        return None

    def user_message(self, item: Row) -> str:
        kw = self.capture(item, self.models[0], tier2=False)
        return "" if kw is None else str(kw["user"])  # no call: matches no frozen digest

    def run_one(self, item: Row, model: str, client: _Recorder) -> Row:
        ans = self.main.ask(*self._ask_args(item, model, client))
        return {"ann_status": ans.status, "payload": json.loads(json.dumps(ans.payload)),
                "counts": dict(ans.counts), "no_call": ans.llm is None}


#: Per task: (what the menu source is, its manifest ``frozen_with`` digest key, its version key).
MENU_SOURCE_KEYS = {"model_roles": ("configs/models.yaml", "models_yaml_sha256", "models_yaml_version"),
                    "technique_roles": ("the taxonomy file", "taxonomy_sha256", "taxonomy_version"),
                    "technique_staged": ("the taxonomy file", "taxonomy_sha256", "taxonomy_version")}


def gold_ids(task: str, g: Row) -> list[str]:
    """The menu ids one gold row names, in order: model_roles ``label.models``, ``label.mentions`` and
    ``label.off_menu_models`` ``node_id`` (when set); technique_roles ``label.techniques[].slug`` and
    ``label.extra_techniques[].hint_technique`` (when set); technique_staged ``label.techniques[].slug`` of the
    rows not flagged ``candidate`` (a ``candidate:<slug>`` row names an entry not yet in the taxonomy)."""
    lab = g.get("label") or {}
    if task == "technique_staged":
        return [str(t["slug"]) for t in lab.get("techniques") or []
                if isinstance(t, Mapping) and t.get("slug") and not t.get("candidate")]
    if task == "model_roles":
        rows = [*(lab.get("models") or []), *(lab.get("mentions") or []), *(lab.get("off_menu_models") or [])]
        return [str(r["node_id"]) for r in rows if isinstance(r, Mapping) and r.get("node_id")]
    return ([str(t["slug"]) for t in lab.get("techniques") or [] if isinstance(t, Mapping) and t.get("slug")]
            + [str(x["hint_technique"]) for x in lab.get("extra_techniques") or []
               if isinstance(x, Mapping) and x.get("hint_technique")])


def fills_digest(fills: Mapping[str, str]) -> str:
    """sha256 over a model_roles item's prompt fills (``main.fills``), JCS-canonical: what
    ``input.fills_digest`` freezes (module docstring)."""
    return sha256_text(jcs(dict(fills)))


def _frozen_with(set_dir: Path | None) -> dict[str, Any]:
    """The set manifest's ``frozen_with`` (empty: no manifest or none recorded)."""
    path = set_dir / "manifest.json" if set_dir is not None else None
    if path is None or not path.is_file():
        return {}
    return dict(json.loads(path.read_text("utf-8")).get("frozen_with") or {})


def _frozen_vocab_digest(set_dir: Path | None) -> str | None:
    """The set manifest's ``frozen_with.models_yaml_sha256`` (None: no manifest or not recorded)."""
    v = _frozen_with(set_dir).get("models_yaml_sha256")
    return None if v is None else str(v)


# ── technique_staged: several calls per document (techpipe D606-D607) ─────────────────────────

#: Call types whose responses are shared across arms within a repeat (C4): their request does not depend on
#: the grading model, so two arms with the same stage model send the same bytes.
SHARED_STAGES = ("prepass", "select")
#: The final (grading) call type of each mode: its responses stay in the arm's own cache.
FINAL_STAGE = {"single": "single", "sweep": "sweep", "staged": "grade", "lexical": "grade"}
#: Estimate assumptions for the answer-dependent parts of a staged document (no call is made): notes per P chunk,
#: P output tokens per note (+ a fixed overhead per call), the tokens one note adds to the sweep call's reading
#: notes and to a selection batch, S output tokens per note, sub-menu entries the selection adds beyond the lexical
#: hits and the tokens of one sub-menu entry (definition + pointers).
EST_NOTES_PER_CHUNK = 30
EST_P_OUT_PER_NOTE, EST_P_OUT_FIXED = 70, 500
EST_NOTE_IN_SWEEP, EST_NOTE_IN_SELECT, EST_S_OUT_PER_NOTE = 45, 110, 40
EST_SUBMENU_EXTRA, EST_SUBMENU_ENTRY_TOKENS = 25, 300


class StagedClient:
    """The client of one (arm, repeat) of ``stack eval technique_staged`` (D607, design C4): P and S calls go to
    ``shared`` (the response cache ``<cache>/shared/r<k>``, keyed by stage model + request bytes + basis like
    every cache entry, so any arm of repeat k sending the same P or S request to the same model reads the same
    answer), the final call to ``arm`` (``<cache>/arm-<mode>-<model>-<pin digest>/r<k>``). A lookup reads only the
    requested stage model's key (D702), so a luna arm's P answer never serves a haiku-pinned P request of another
    arm (``extract_staged`` falls back to luna). Both clients carry the run id, so one ledger and one
    ``--max-usd`` budget cover them."""

    def __init__(self, arm: Any, shared: Any, stage_of: Callable[[Mapping[str, Any]], str]) -> None:
        self.arm, self.shared, self.stage_of = arm, shared, stage_of
        self._llm = arm._llm

    def client_for(self, stage: str) -> Any:
        return self.shared if stage in SHARED_STAGES else self.arm

    def complete(self, **kw: Any) -> Any:
        return self.client_for(self.stage_of(kw)).complete(**kw)


class CallRecorder(_Recorder):
    """:class:`stack.evals.harness._Recorder` that also keeps, per call, its stage and, for a failed call, the
    cost and attempts the error carried when it was raised: the stage later overwrites the propagating error's
    ``cost_usd`` / ``attempts`` with the whole ask's (``main._Ask.fail``), which would count earlier calls twice."""

    def __init__(self, client: StagedClient) -> None:
        super().__init__(client)  # type: ignore[arg-type]
        self.staged = client
        self.records: list[dict[str, Any]] = []

    def complete(self, **kw: Any) -> Any:
        from stack.llm import LlmError

        stage = self.staged.stage_of(kw)
        try:
            res = self.staged.complete(**kw)
        except LlmError as exc:
            self.calls.append(exc)
            attempts = tuple(getattr(exc, "attempts", ()) or ())
            self.records.append({"stage": stage, "model": kw.get("model"), "error": exc,
                                 "cost_usd": float(getattr(exc, "cost_usd", 0.0) or 0.0), "attempts": attempts})
            raise
        self.calls.append(res)
        self.records.append({"stage": stage, "result": res})
        return res


class TechniqueStagedAdapter(AnnotatorAdapter):
    """``stack eval technique_staged``: production ``workflows/annotate/technique_staged/main.ask`` on the frozen
    whole texts of ``evals/technique_staged/<set>/`` (D606-D607).

    **Pins.** Per model of ``--models`` (the grading model, pin ``model``): ``{name, model, **plan, **--pin}``,
    ``name`` from ``--prompt`` (default ``technique-staged-v1``); ``plan`` = the plan's
    ``prompts.annotate.technique_staged`` keys other than ``name``, ``model`` and ``template_digest`` when its name
    is the run's; ``--pin KEY=VALUE`` sets ``mode`` (required by the stage), ``prepass_model``, ``select_model``,
    ``role``, ``taxonomy``, ``max_tokens`` (``name`` and ``model`` have their own options). A stage model neither pin
    sets is the grading model (``main.pin_of``); ``run.json`` ``stage_models`` records the effective model of every
    call type per grading model. Every stage model must be priced and not retired in ``llm.yaml``. All pins of a run
    share one mode.

    **Input check.** The whole text against ``input.full_text_digest``; ``input_digest`` (the ``single`` user
    message) when the pin's prompt set is the frozen one; and per mode the parts of ``input.staged``
    (``main.freeze_info``) the mode's calls depend on: ``single`` the single user digest; ``sweep`` the P chunk
    digests; ``staged`` the P chunk digests and the lexical digest; ``lexical`` the lexical user digest; the
    segmentation digest for every mode but ``single``. User digests are compared only under the frozen prompt set
    (the segmentation and lexical digests always). The S and final messages of sweep and staged depend on the P
    and S answers and cannot be frozen.

    **Cache and accounting** (:class:`StagedClient`, :class:`CallRecorder`, :meth:`accounting`): rows carry
    ``calls`` (per call: ``stage`` prepass | select | grade | single | sweep, ``model``, ``status``,
    ``from_response_cache``, ``cache`` shared | arm, tokens in / out, the provider's ``prompt_tokens``,
    ``cached_tokens``, ``cache_write_tokens``, ``reasoning_tokens`` from the response-cache entry, ``cost_usd``
    spent now, ``call_cost_usd`` = the provider-reported cost of the call, also when this run read it from the
    cache, ``latency_ms`` now and ``call_latency_ms`` as made), ``standalone_cost_usd`` (sum of ``call_cost_usd``:
    the arm's cost without cross-arm or re-run cache reuse) and ``standalone_latency_ms``, beside the harness's
    standard fields (``cost_usd`` = actual spend).
    """

    #: Plan-pin keys ``--pin`` may not set (each has its own option).
    OWN_OPTION_KEYS = {"name": "--prompt", "model": "--models"}

    def __init__(self, config: StackConfig, plan: Any, prompt: str | None, models: list[str], *,
                 pin_overrides: Mapping[str, Any] | None = None) -> None:
        from stack.evals.harness import PLAN_PROMPT_KEY

        task = "technique_staged"
        overrides = dict(pin_overrides or {})
        own = sorted(k for k in overrides if k in self.OWN_OPTION_KEYS)
        if own:
            raise EvalError(f"--pin {own}: set with {', '.join(self.OWN_OPTION_KEYS[k] for k in own)} instead")
        main = workflow_module(task)
        plan_pin = dict((plan.prompts.get(PLAN_PROMPT_KEY[task]) if plan is not None else None) or {})
        name = str(prompt or plan_pin.get("name") or DEFAULT_PROMPT[task])
        carried = ({k: v for k, v in plan_pin.items() if k not in ("name", "model", "template_digest")}
                   if plan_pin.get("name", main.DEFAULT_NAME) == name else {})
        Adapter.__init__(self, task, config, name, models)
        self.main = main
        self.pin_overrides = overrides
        self.plan_pins = {}
        self.prompt_pins = {}
        llm = config.llm
        for m in models:
            p: dict[str, Any] = {"name": name, "model": m, **carried, **overrides}
            try:
                self.prompt_pins[m] = main.prompt(p, llm)
                pin = main.pin_of(p, llm)
            except Exception as exc:  # StageError: a bad pin (no mode, an unknown key, a key the mode does not use)
                raise EvalError(f"{task}: {exc}") from exc
            stage_models = {pin.model_of(c) for c in main.CALLS[pin.mode]}
            unpriced = sorted(x for x in stage_models if x not in llm.pricing.models)
            if unpriced:
                raise EvalError(f"{task}: stage models {unpriced} have no llm.yaml pricing entry (D173)")
            retired = sorted(x for x in stage_models if llm.is_retired(x))
            if retired:
                raise EvalError(f"{task}: stage models {retired} are retired in llm.yaml")
            self.plan_pins[m] = p
        modes = {main.pin_of(p, llm).mode for p in self.plan_pins.values()}
        if len(modes) != 1:
            raise EvalError(f"{task}: the pins name several modes {sorted(modes)}")
        self.mode = modes.pop()
        self._shared: dict[int, Any] = {}
        self._shared_lock = threading.Lock()
        self._fills: dict[str, dict[str, Any]] = {}

    # ── pins and the input check ─────────────────────────────────────────────────────────────

    def pin(self, model: str) -> Any:
        return self.main.pin_of(self.plan_pins[model], self.config.llm)

    def arm_slug(self, model: str) -> str:
        """The arm's cache directory name: mode, grading model and a digest of the whole effective pin."""
        p = self.plan_pins[model]
        digest = sha256_text(jcs({k: v for k, v in p.items() if k != "template_digest"}))[:10]
        return f"arm-{self.mode}-{model.replace('/', '__').replace(':', '_')}-{digest}"

    def menu_source(self) -> dict[str, str]:
        cat = self.main.catalogue(self.pin(self.models[0]).taxonomy)
        return {"sha256": str(cat.sha256), "version": str(cat.version)}

    def menu_ids(self) -> frozenset[str]:
        return frozenset(self.main.catalogue(self.pin(self.models[0]).taxonomy).slugs)

    def stage_models(self) -> dict[str, dict[str, str]]:
        """Per grading model, the effective model of each call type of the mode (pins plus defaults)."""
        return {m: {c: self.pin(m).model_of(c) for c in self.main.CALLS[self.mode]} for m in self.models}

    def provenance(self) -> Row:
        out = super().provenance()
        out["mode"] = self.mode
        out["stage_models"] = self.stage_models()
        out["cache_layout"] = {"shared": "shared/r<k>", "shared_stages": list(SHARED_STAGES),
                               "arms": {m: f"{self.arm_slug(m)}/r<k>" for m in self.models}}
        return out

    def frozen_prompt_set(self) -> str | None:
        v = _frozen_with(self.set_dir).get("prompt_set")
        return None if v is None else str(v)

    def checks_user_message(self) -> bool:
        frozen = self.frozen_prompt_set()
        return frozen is not None and all(p["name"] == frozen for p in self.plan_pins.values())

    def user_message(self, item: Row) -> str:
        """The ``single`` user message (what ``input_digest`` freezes, for every mode)."""
        return str(self.main.user_message(self.full_text(item), mode="single", name=self.prompt_name))

    def staged_fills(self, item: Row) -> dict[str, Any]:
        iid = item["item_id"]
        if iid not in self._fills:
            pin = self.pin(self.models[0])
            self._fills[iid] = dict(self.main.freeze_info(self.full_text(item), name=pin.name,
                                                          taxonomy_path=pin.taxonomy))
        return self._fills[iid]

    def frozen_problems(self, item: Row) -> list[str]:
        text = self.full_text(item)
        got = sha256_text(text)
        if got != item["input"]["full_text_digest"]:
            return [f"whole text (texts/{item['item_id']}.txt.gz) hashes to {got[:12]}, frozen "
                    f"full_text_digest {item['input']['full_text_digest'][:12]}"]
        frozen = item["input"].get("staged")
        if not isinstance(frozen, Mapping):
            return ["no input.staged (the set was not frozen for technique_staged)"]
        now = self.staged_fills(item)
        keys: list[str] = [] if self.mode == "single" else ["segmentation_digest"]
        if self.mode == "staged":
            keys.append("lexical_digest")
        out = [f"{k} {str(now.get(k))[:12]}, frozen {str(frozen.get(k))[:12]}" for k in keys
               if now.get(k) != frozen.get(k)]
        if self.checks_user_message():
            user = {"single": ["single"], "sweep": ["prepass"], "staged": ["prepass"], "lexical": ["lexical"]}
            for k in user[self.mode]:
                if now["user_digests"].get(k) != (frozen.get("user_digests") or {}).get(k):
                    out.append(f"{k} user message(s) differ from the frozen input.staged.user_digests.{k}")
        return out

    # ── clients, recorder, accounting ────────────────────────────────────────────────────────

    def stage_of(self, kw: Mapping[str, Any]) -> str:
        """The call type of one ``complete`` call, from the schema object the stage passes (``main.SCHEMAS``, or
        one of ``main.FINAL_SCHEMAS`` for the final single / sweep call)."""
        schema = kw.get("schema")
        m = self.main
        for call, obj in (("prepass", m.P_SCHEMA), ("select", m.S_SCHEMA), ("grade", m.G_SCHEMA)):
            if schema is obj:
                return call
        if any(schema is obj for obj in getattr(m, "FINAL_SCHEMAS", (m.SINGLE_SCHEMA,))):
            return FINAL_STAGE[self.mode]
        raise EvalError("technique_staged: a call with a schema that is none of main.SCHEMAS")

    def open_client(self, cfg: StackConfig, *, run_id: str, stage: str, model: str, repeat: int,
                    store_root: Path, budget_usd: float) -> StagedClient:
        from stack.llm import open_client

        if self.cache_root is None:
            raise EvalError("technique_staged: the adapter has no cache root")
        with self._shared_lock:
            if repeat not in self._shared:
                self._shared[repeat] = open_client(cfg, run_id=run_id, stage=stage, budget_usd=budget_usd,
                                                   store_root=self.cache_root / "shared" / f"r{repeat}")
            shared = self._shared[repeat]
        arm = open_client(cfg, run_id=run_id, stage=stage, budget_usd=budget_usd,
                          store_root=self.cache_root / self.arm_slug(model) / f"r{repeat}")
        return StagedClient(arm, shared, self.stage_of)

    def recorder(self, client: Any) -> CallRecorder:
        return CallRecorder(client)

    def accounting(self, rec: _Recorder) -> Row:
        """The row's standard fields and its per-call detail (class docstring), from :class:`CallRecorder`."""
        from stack.evals.score import entry_usage, row_usage
        from stack.llm import LlmResult
        from stack.llm.cache import cache_key

        assert isinstance(rec, CallRecorder)
        price_for = rec.staged._llm.price_for
        calls: list[Row] = []
        entries: list[tuple[Mapping[str, Any] | None, bool]] = []
        attempts: list[Any] = []
        for r in rec.records:
            stage = r["stage"]
            where = "shared" if stage in SHARED_STAGES else "arm"
            if "error" in r:
                exc = r["error"]
                attempts.extend(r["attempts"])
                calls.append({"stage": stage, "model": r["model"], "cache": where, "status": exc.status,
                              "error_class": exc.error_class, "from_response_cache": False, "tokens_in": 0,
                              "tokens_out": 0, "prompt_tokens": None, "cached_tokens": None,
                              "cache_write_tokens": None, "reasoning_tokens": None, "cost_usd": r["cost_usd"],
                              "call_cost_usd": r["cost_usd"], "latency_ms": sum(a.latency_ms for a in r["attempts"]),
                              "call_latency_ms": sum(a.latency_ms for a in r["attempts"]),
                              "attempts": len(r["attempts"])})
                continue
            res: LlmResult = r["result"]
            attempts.extend(res.attempts)
            cache = getattr(rec.staged.client_for(stage), "cache", None)
            entry = cache.get(cache_key(res.model, res.rendered_digest, res.basis_digest)) if cache is not None else None
            entries.append((entry, res.is_cache_hit))
            u = entry_usage(entry, price_for(str(entry.get("model")))) if entry is not None else None
            latency = sum(a.latency_ms for a in res.attempts)
            if res.is_cache_hit:
                call_cost = u["cost_usd"] if u is not None else None
                call_latency = int(entry.get("latency_ms") or 0) if entry is not None else None
            else:
                call_cost, call_latency = res.cost_usd, latency
            calls.append({"stage": stage, "model": res.model, "cache": where, "status": "ok", "error_class": None,
                          "from_response_cache": res.is_cache_hit, "tokens_in": res.tokens_in,
                          "tokens_out": res.tokens_out,
                          **{k: (u[k] if u is not None else None)
                             for k in ("prompt_tokens", "cached_tokens", "cache_write_tokens", "reasoning_tokens")},
                          "cost_usd": res.cost_usd, "call_cost_usd": call_cost, "latency_ms": latency,
                          "call_latency_ms": call_latency, "attempts": len(res.attempts),
                          "degraded": res.degraded})
        results = [r["result"] for r in rec.records if "result" in r]
        cost = sum(c["cost_usd"] for c in calls)
        final = [r["result"] for r in rec.records if "result" in r and r["stage"] not in SHARED_STAGES]
        standalone = (None if any(c["call_cost_usd"] is None for c in calls)
                      else sum(float(c["call_cost_usd"]) for c in calls))
        sa_latency = (None if any(c["call_latency_ms"] is None for c in calls)
                      else sum(int(c["call_latency_ms"]) for c in calls))
        return {
            **row_usage(entries, cost, price_for),
            "answer_model": final[-1].model if final else None,
            "cost_usd": cost, "reserved_usd": sum(a.reserved_usd for a in attempts),
            "tokens_in": sum(r.tokens_in for r in results), "tokens_out": sum(r.tokens_out for r in results),
            "latency_ms": sum(a.latency_ms for a in attempts),
            "cache_hit": bool(results) and len(results) == len(rec.records) and all(r.is_cache_hit for r in results),
            "schema_degraded": any(r.degraded for r in results), "unwrapped": any(r.unwrapped for r in results),
            "attempts": len(attempts), "attempt_errors": [a.error_class for a in attempts if a.error_class],
            "calls": calls, "standalone_cost_usd": standalone, "standalone_latency_ms": sa_latency,
        }

    # ── estimate ─────────────────────────────────────────────────────────────────────────────

    def capture_calls(self, item: Row, model: str) -> list[tuple[str, dict[str, Any]]]:
        """``(stage, complete kwargs)`` of the calls ``ask`` makes when every P chunk answers with no notes:
        each P chunk's request exactly, then the final call (exact for single and lexical; for sweep without
        reading notes, for staged with the lexical sub-menu alone; :meth:`estimate` adds the rest)."""
        adapter = self

        class Probe:
            def __init__(self) -> None:
                self.calls: list[tuple[str, dict[str, Any]]] = []

            def complete(self, **kw: Any) -> Any:
                stage = adapter.stage_of(kw)
                self.calls.append((stage, kw))
                if stage == "prepass":
                    return SimpleNamespace(parsed={"mentions": [], "overflow": False}, model=kw.get("model"))
                raise _Captured

        probe = Probe()
        try:
            self.main.ask(*self._ask_args(item, model, probe))
        except _Captured:
            return probe.calls
        raise AssertionError("technique_staged ask returned without making its final call")

    def _select_kwargs(self, item: Row, model: str) -> dict[str, Any]:
        """One selection call's kwargs with no notes (its system part and the document opening)."""
        m, pin = self.main, self.pin(model)
        cat = m.catalogue(pin.taxonomy)
        seg = m.segment.segment(self.full_text(item))
        pf = m.prompt_file(pin.name, "select")
        return {"role": pin.role, "model": pin.select_model, "template": m.system_template(pf, "select", cat),
                "refs": {}, "user": pf.user(m.stages.select_fills(seg.opening, [], m.stages.batch_note(1, 1))),
                "schema": m.S_SCHEMA, "max_tokens": pin.max_tokens, "license_class": license_of(item),
                "basis_digest": str(item["input"]["text_digest"])}

    def estimate(self, items: Sequence[Row], models: Sequence[str], repeat: int, *, probe: Any,
                 cache_dir: Callable[[str, int], Path]) -> Row:
        """Reservation and expected cost of the planned calls: P and single / lexical requests exactly (and
        checked against the shared / arm caches), the answer-dependent parts by the ``EST_*`` assumptions."""
        from stack.evals.harness import EXPECTED_OUT_TOKENS, _request_of
        from stack.llm import reserve_tokens_in
        from stack.llm.cache import ResponseCache, cache_key

        assert self.cache_root is not None
        llm = probe._llm
        final_out = EXPECTED_OUT_TOKENS[self.task]
        per_model: dict[str, Row] = {}
        reserve_total = expected_total = standalone_total = 0.0
        max_outs: dict[str, int] = {}
        for model in models:
            max_out = max_outs[model] = int(self.pin(model).max_tokens)  # the pin's output cap (default 16,000)
            shared = [ResponseCache.for_store(self.cache_root / "shared" / f"r{k}") for k in range(1, repeat + 1)]
            arm = [ResponseCache.for_store(self.cache_root / self.arm_slug(model) / f"r{k}")
                   for k in range(1, repeat + 1)]
            pm: Row = {"calls": 0, "cached": 0, "egress_skipped": 0, "reserve_usd": 0.0, "expected_usd": 0.0,
                       "standalone_expected_usd": 0.0, "reserve_per_call_usd": 0.0, "per_stage": {}}
            reserves: list[float] = []

            def add(stage: str, req_model: str, tin: int, tout: int, n: int, cached: int,
                    max_out: int = max_out) -> None:
                price = llm.price_for(req_model)
                st = pm["per_stage"].setdefault(stage, {"calls": 0, "cached": 0, "tokens_in": 0, "reserve_usd": 0.0,
                                                        "expected_usd": 0.0, "standalone_expected_usd": 0.0})
                fresh = n - cached
                st["calls"] += n
                st["cached"] += cached
                st["tokens_in"] += tin * n
                st["reserve_usd"] += price.cost(tin, max_out) * fresh
                st["expected_usd"] += price.cost(tin, tout) * fresh
                st["standalone_expected_usd"] += price.cost(tin, tout) * n
                pm["calls"] += n
                pm["cached"] += cached
                pm["reserve_usd"] += price.cost(tin, max_out) * fresh
                pm["expected_usd"] += price.cost(tin, tout) * fresh
                pm["standalone_expected_usd"] += price.cost(tin, tout) * n
                reserves.append(price.cost(tin, max_out))

            for it in items:
                captured = self.capture_calls(it, model)
                built = [(stage, kw, _request_of(probe, kw)) for stage, kw in captured]
                if any(b is None for _, _, b in built):
                    pm["egress_skipped"] += repeat
                    continue
                n_chunks = sum(stage == "prepass" for stage, _, _ in built)
                notes = n_chunks * EST_NOTES_PER_CHUNK
                for stage, kw, b in built:
                    assert b is not None
                    req, rdig, basis = b
                    tin = reserve_tokens_in(req)
                    key = cache_key(req.model, rdig, basis)
                    if stage == "prepass":
                        cached = sum(c.get(key) is not None for c in shared)
                        add(stage, req.model, tin, EST_P_OUT_PER_NOTE * EST_NOTES_PER_CHUNK + EST_P_OUT_FIXED,
                            repeat, cached)
                    elif stage in ("single", "lexical") or (stage == "grade" and self.mode == "lexical"):
                        add(stage, req.model, tin, final_out, repeat, sum(c.get(key) is not None for c in arm))
                    elif stage == "sweep":
                        add(stage, req.model, tin + notes * EST_NOTE_IN_SWEEP, final_out, repeat, 0)
                    else:  # staged grading: + the entries the selection adds
                        add(stage, req.model, tin + EST_SUBMENU_EXTRA * EST_SUBMENU_ENTRY_TOKENS, final_out, repeat, 0)
                if self.mode == "staged" and notes:
                    sb = _request_of(probe, self._select_kwargs(it, model))
                    assert sb is not None
                    base = reserve_tokens_in(sb[0])
                    batch = int(self.main.stages.SELECT_BATCH)
                    for k in range(0, notes, batch):
                        n = min(batch, notes - k)
                        add("select", sb[0].model, base + n * EST_NOTE_IN_SELECT, n * EST_S_OUT_PER_NOTE, repeat, 0)
            pm["reserve_per_call_usd"] = sum(reserves) / len(reserves) if reserves else 0.0
            per_model[model] = pm
            reserve_total += pm["reserve_usd"]
            expected_total += pm["expected_usd"]
            standalone_total += pm["standalone_expected_usd"]
        return {"items": len(items), "models": list(models), "repeats": repeat, "mode": self.mode,
                "planned_calls": sum(pm["calls"] for pm in per_model.values()),
                "expected_out_tokens": final_out, "per_model": per_model, "reserve_usd": reserve_total,
                "expected_usd": expected_total, "standalone_expected_usd": standalone_total,
                "assumptions": {"notes_per_chunk": EST_NOTES_PER_CHUNK, "p_out_per_note": EST_P_OUT_PER_NOTE,
                                "p_out_fixed": EST_P_OUT_FIXED, "note_in_sweep": EST_NOTE_IN_SWEEP,
                                "note_in_select": EST_NOTE_IN_SELECT, "s_out_per_note": EST_S_OUT_PER_NOTE,
                                "submenu_extra": EST_SUBMENU_EXTRA, "submenu_entry_tokens": EST_SUBMENU_ENTRY_TOKENS,
                                "final_out": final_out,
                                "max_tokens": max(max_outs.values(), default=int(self.main.MAX_TOKENS))}}

    def format_estimate(self, est: Row, max_usd: float) -> str:
        lines = [(f"estimate ({est['mode']}): {est['items']} items x {len(est['models'])} models x {est['repeats']} "
                  f"repeats = {est['planned_calls']} calls; reservations ${est['reserve_usd']:.4f} (--max-usd "
                  f"${max_usd:.2f}), expected ${est['expected_usd']:.4f} (standalone, no cache: "
                  f"${est['standalone_expected_usd']:.4f})"),
                 ("  P and single/lexical requests exact; S, sweep's reading notes and staged's extra sub-menu "
                  f"entries assumed ({est['assumptions']['notes_per_chunk']} notes per chunk); output ~"
                  f"{est['expected_out_tokens']} tokens per grading call")]
        for m, pm in est["per_model"].items():
            lines.append(f"  {m:<36} calls {pm['calls']:4d}  cached {pm['cached']:4d}  egress-skipped "
                         f"{pm['egress_skipped']:3d}  reserve ${pm['reserve_usd']:.4f}  expected ${pm['expected_usd']:.4f}")
            for st, v in pm["per_stage"].items():
                lines.append(f"    {st:<8} calls {v['calls']:4d}  cached {v['cached']:4d}  tokens in "
                             f"{v['tokens_in']:>9,d}  reserve ${v['reserve_usd']:.4f}  expected ${v['expected_usd']:.4f}")
        return "\n".join(lines)


# ── model_mentions (no LLM) ──────────────────────────────────────────────────────────────────


def mentions_rows(items: Sequence[Row], set_dir: Path, scan: Callable[[str], Row]) -> list[Row]:
    """One row per item: the production scan of its frozen whole text (refused when the text no
    longer hashes to ``input.full_text_digest``) and whether it differs from the frozen payload."""
    rows, bad = [], []
    for it in items:
        text = read_frozen_text(set_dir, it)
        if sha256_text(text) != it["input"]["full_text_digest"]:
            bad.append(it["item_id"])
            continue
        payload = scan(text)
        frozen = (it["input"].get("model_mentions") or {}).get("payload")
        rows.append({"item_id": it["item_id"], "doc_id": it["doc_id"], "split": it["split"],
                     "strong": [e["node_id"] for e in payload["mentions"]],
                     "weak": [e["node_id"] for e in payload["weak"]],
                     "ambiguous": len(payload["ambiguous"]), "payload": payload,
                     "frozen_differs": frozen != payload})
    if bad:
        raise EvalError(f"{len(bad)} whole text(s) no longer hash to their full_text_digest (a new set is "
                        f"needed): {', '.join(bad[:10])}")
    return rows


def run_mentions(set_id: str, split: str, *, items: Sequence[str] | None = None, limit: int | None = None,
                 gold: str | None = None, evals_root: Path = DEFAULT_EVALS, out_root: Path = DEFAULT_OUT,
                 config: StackConfig | None = None, echo: Callable[[str], None] = print) -> Path:
    """``stack eval model_mentions`` (module docstring); returns the run directory."""
    from stack.annotate.stage import stack_config
    from stack.evals import score

    cfg = config if config is not None else stack_config()
    eset = load_set(evals_root, MENTIONS_SET_TASK, set_id)
    chosen = select_items(eset.items, split, items, limit)
    mm = workflow_module("model_mentions")
    compiled = mm.scanner_for(cfg.config_dir)
    vocab = compiled.vocab
    rows = mentions_rows(chosen, eset.dir, lambda text: mm.scan_payload(compiled.scanner, text))
    try:
        gold_file, gold_rows = score.load_gold(eset.dir, gold or score.default_gold(eset.dir))
    except FileNotFoundError as exc:
        raise EvalError(str(exc)) from exc
    block = score.mentions_block(rows, gold_rows, lambda a, b: bool(mm.is_ancestor(vocab.nodes, a, b)))
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    root = out_root / "model_mentions" / set_id
    run_dir = root / f"{stamp}_{split}"
    n = 1
    while run_dir.exists():
        n += 1
        run_dir = root / f"{stamp}_{split}-{n}"
    run_dir.mkdir(parents=True)
    run = {"task": "model_mentions", "set": set_id, "set_dir": str(eset.dir.resolve()), "split": split,
           "items": [r["item_id"] for r in rows], "gold": gold_file, "git_head": _git_head(),
           "vocab_digest": vocab.digest, "vocab_stamp": vocab.stamp,
           "frozen_vocab_digest": _frozen_vocab_digest(eset.dir),
           "set_items_digest": eset.items_digest, "started": stamp}
    summary = {**run, "mentions": block}
    md = mentions_markdown(summary)
    (run_dir / "run.json").write_text(json.dumps(run, indent=2, sort_keys=True) + "\n", "utf-8")
    write_jsonl(run_dir / "rows.jsonl", rows)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", "utf-8")
    (run_dir / "summary.md").write_text(md + "\n", "utf-8")
    echo(md)
    echo(f"run: {run_dir}")
    return run_dir


def mentions_markdown(s: Row) -> str:
    from stack.evals.score import _f, _table

    b = s["mentions"]
    same = "same as" if s["vocab_digest"] == s["frozen_vocab_digest"] else "DIFFERS from"
    lines = [f"# stack eval model_mentions: {s['set']} / {s['split']}", "",
             (f"Production `match.scan_payload` over {b['n']} frozen whole texts; vocabulary `{s['vocab_stamp']}` ({same} "
              f"the freeze's); gold {'yes (' + str(b['gold_items']) + ' items, ' + str(s['gold']) + ')' if b['gold_items'] else 'none'}."),
             (f"Scan differs from the frozen `input.model_mentions.payload` on {b['frozen_differs']} item(s)"
              + (f": {', '.join(b['frozen_differs_items'])}" if b["frozen_differs_items"] else "") + "."), ""]
    if b["gold_items"]:
        lines += [("Micro over (item, node). *lenient*: a predicted node matches a gold node when one is the "
                   "other's ancestor in the vocabulary (family vs model). Out-of-vocabulary gold mentions bound "
                   "recall but the matcher cannot know them (not in the gold node sets)."), ""]
        rows = []
        for key, label in (("strong", "strong"), ("strong_weak", "strong ∪ weak"),
                           ("strong_lenient", "strong, lenient"), ("strong_weak_lenient", "strong ∪ weak, lenient")):
            m = b[key]
            rows.append([label, str(m["pred"]), str(m["gold"]), _f(m["precision"]), _f(m["recall"]), _f(m["f1"])])
        lines += _table(["prediction", "pred nodes", "gold nodes", "P", "R", "F1"], rows) + [""]
        lines += [f"Gold out-of-vocabulary mentions: {b['gold_oov']}; ambiguous spans in the scan: {b['ambiguous']}.",
                  "", "### per item (strong; FP / FN, strict)", ""]
        for it in b["per_item"]:
            if it["fp"] or it["fn"]:
                lines.append(f"- {it['item_id']}: FP {', '.join(it['fp']) or '—'}; FN {', '.join(it['fn']) or '—'}"
                             + (f"; weak {', '.join(it['weak'])}" if it["weak"] else ""))
        lines.append("")
    return "\n".join(lines)


__all__ = ["BASIS_KIND", "WORKFLOW_MODULES", "AnnotatorAdapter", "CallRecorder", "StagedClient", "TechniqueStagedAdapter",
           "fills_digest", "mentions_rows", "run_mentions"]
