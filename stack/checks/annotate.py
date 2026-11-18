"""V7 ``V-wf``, V8 ``V-evidence``, X6 ``policy_pins``, X7 ``basis_currency``, X8
``selection_recompute`` (Appendix B; §3.1 ``annotate.<wf>``; D14, D19, D99). All store scope:
each pairs a ``<wf>`` file with the ``<wf>_evidence`` file of the same run (``file_id =
<run_id>.<wf>_evidence``, D51) or with the ``corpus`` file its header ``inputs`` name.
``triage_evidence`` (written by ``triage``, one question per row) is checked by V8 only.

**Annotation records** (registry ``produced_by``): a ``<record>`` file is a ``<wf>`` file whose
evidence is its producer's: every pairing below that names "the same run's evidence file" reads
``<run_id>.<producer>_evidence`` for a record (``PayloadSpec.evidence_family``), and no
``<record>_evidence`` exists. Record-specific branches: V7 resolves a record's ``evidence_id`` in
the producer's evidence file; V8 requires that file (not a ``<record>_evidence``) and recomputes
an evidence row's ``result_digest`` over its payload plus its non-NULL record columns
(:func:`stack.annotate.evidence.evidence_payload`); X6 checks a record's evidence against its
producer's pin, and that the pin produces the record; X8 reselects the record from the producer's
evidence with the record's own overrides (a decided evidence row without the record's payload is
a finding).

**V7** (every ``<wf>`` file): the header's and every row's ``basis_kind`` is the registry's; a
non-NULL ``evidence_id`` resolves in the same run's evidence file (a pure workflow, registry
``evidence: false``, names none); an ``ok``/``empty`` row of an ``evidence: true`` workflow
whose ``origin`` is not ``human`` has an ``evidence_id`` or lists the override ids that supplied
its payload (an ``ai_check`` override, D717); a ``human`` row lists the override ids it applied.

**V8** (every ``<wf>_evidence`` file, ``triage_evidence`` included, and every ``<wf>`` file):
``work_digest`` and ``result_digest`` recompute from the row
(:func:`stack.annotate.evidence.with_digests`; ``result_digest`` NULL unless ``ok``/``empty``);
``basis_kind`` is the registry's, ``item_key = ''`` (one invocation per document); an evidence
file has a same-run ``<wf>`` file, and a ``<wf>`` file of an ``evidence: true`` workflow a
same-run evidence file (evidence iff ``evidence: true``). ``triage_evidence`` (D159 (4), D166
(C)) recomputes under :data:`stack.annotate.evidence.TRIAGE_EVIDENCE_SPEC` (payload = the
``question, verdict, confidence, reasoning`` columns; work key over the question's
``config_digest``); its workflow output is the ``documents`` part, not a ``<wf>`` file, so the
same-run pairing is not checked for it.

**X scope** (X6-X8, "verify over a build's set"): with a reference build record (the one
``verify`` is given, else ``latest``, the newest ``ok`` or ``incomplete`` build, D703), the ``<wf>``
files its ``files`` names (D17 key = the workflow); without one, the newest ``<wf>`` file per workflow (by
``finished_at``) among those whose header ``inputs`` name a ``corpus`` file (every
``annotate.<wf>`` run's do; a hand-made file without one is X2's concern). Each is checked against the ``corpus`` file of its header ``inputs`` and its
same-run evidence file; one that names no corpus file, or whose corpus or evidence file is not
under ``data/``, is a finding of X7/X8.

**X6** (``ai_check`` evidence rows with status ``ok``/``empty`` that the ``<wf>`` file selects or
that its run wrote) judges the evidence against **the pin the run asked under** (round 5, R1-1 /
R2-4: a property of the stored build, not of today's config files): the run record's
``prompt_pin`` (:func:`stack.annotate.stage.pin_record`, written by ``annotate.<wf>`` runs since
then). ``prompt_name`` is the pin's, ``template_digest`` one of its ``template_digests``, a record
file one its ``produced`` lists; and the pin must hash to the file header's ``config_digest`` (a
record that does not is a hard finding). A run record without ``prompt_pin`` (an older run) falls
back to today's pin (``prompts.annotate.<wf>`` of the plan the header names, found as ``stack
build`` finds plans; :func:`stack.annotate.resolve_pin`) **only when** that pin's config digest
equals the header's ``config_digest`` (so it is the run's pin); otherwise the evidence is not
checked against a pin and a **warn** finding says so (with how many rows today's prompt name or
templates would reject, for orientation). A second warn finding says when the plan's
current pin differs from the one the run recorded ("the next build re-asks"); X6 then reads
``warn``, not ``failed`` (:func:`stack.checks.result`: a hard check whose findings are all
warn-level). A plan or workflow that cannot be loaded is a hard finding when the run recorded no
pin (nothing else to judge by), a warn finding otherwise.

**Accepted rows** (D758, :mod:`stack.plan` "Accepted configs"): a selected row whose evidence (an
``evidence_id``; a row the overrides supplied whole has none and its own digest) has a ``config_digest`` other than
the header's must be of a config the run accepted (the run record's ``selection.accepted``, which the plan line
gave), whatever its origin (hard); an ``ai_check`` one is judged against **its own** run's pin (the evidence row's
``run_id`` → that run record's ``prompt_pin``, which must hash to the row's ``config_digest``), not the header's.
The selection's ``target`` must be the header's config and its digest must recompute (hard). A run record without
``selection`` accepts nothing.

**Joined reads** (D235): X7 and X8 read the ``corpus`` rows as the stage saw them, joined with the
annotations its header ``inputs`` name besides ``corpus`` (every input that is a registered
annotation, :func:`stack.annotate.inputs.header_reads`) through
:func:`stack.annotate.inputs.join_reads`; a named read file that is not readable is a finding.

**X7**: every row's ``is_basis_current`` equals its recomputation against its ``corpus`` row
(:func:`stack.annotate.basis.is_basis_current`, the function ``fold.build``'s mapping M uses).

**X8**: :func:`stack.annotate.select.select` over the corpus rows of the file's documents, the
same-run evidence, the ``overrides`` view at the head the header ``inputs`` pinned (none pinned: no
overrides; target ``<wf>``, revokes applied), the header ``config_digest`` and the accepted configs the run
applied (its run record's ``selection``, checked as in X6; none recorded: none) and, when that ``selection`` says
``same_basis: true`` (D807), another supplier's same-basis answers, reproduces the file: same ``(doc_id, item_key)`` set, every column equal except
``is_basis_current`` (X7's) and, on rows without evidence, the run stamps ``decided_at`` and
``run_id``. Rows the ask substage did not reach (no evidence, ``not_attempted``) keep their
recorded ``error_class``. An override added after the run does not move X8 (the pinned head
does not move); an unreadable pinned overrides view is a finding. Pure workflows (``evidence: false``) have nothing to recompute from and are not checked.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from stack.annotate.basis import is_basis_current
from stack.annotate.evidence import OK_LIKE, TRIAGE_EVIDENCE_SPEC, with_digests
from stack.checks import Finding, register
from stack.checks.chain import BATCH_ROWS, DataFile, StoreView
from stack.schemas.families import CHAIN_WORKFLOWS
from stack.schemas.payloads.registry import REGISTRY, PayloadSpec
from stack.schemas.vocab import ANN_ORIGINS

_EXAMPLES = 5
_STAMPS: frozenset[str] = frozenset({"decided_at", "run_id"})


def _finding(check: str, message: str, bad: list[str], level: str = "hard") -> list[Finding]:
    return [Finding(check, level, message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


def _annotate_workflows() -> dict[str, PayloadSpec]:
    return {w: s for w, s in REGISTRY.items() if w not in CHAIN_WORKFLOWS}


def _rows(view: StoreView, df: DataFile | None) -> list[dict[str, Any]] | None:
    if df is None:
        return None
    tbl = view.table(df)
    return None if tbl is None else tbl.to_pylist()


def _same_run(view: StoreView, df: DataFile, family: str) -> DataFile | None:
    return view.file(f"{df.header.get('run_id')}.{family}")


def _wf_files(view: StoreView) -> Iterator[tuple[DataFile, PayloadSpec]]:
    wfs = _annotate_workflows()
    for df in view.files:
        if df.spec is not None and df.spec.kind == "wf" and df.family in wfs:
            yield df, wfs[df.family]


# ---------------------------------------------------------------- V7
@register("V7", "V-wf", scope="store")
def v_wf(view: StoreView) -> list[Finding]:
    """V7 (module docstring)."""
    bad: list[str] = []
    for df, spec in _wf_files(view):
        if not view.in_scope(df):  # the files part's scope (stack.checks.verify)
            continue
        rows = _rows(view, df)
        if rows is None:  # S1's finding
            continue
        name = df.key
        if df.header.get("basis_kind") != spec.basis_kind:
            bad.append(f"{name}: header basis_kind {df.header.get('basis_kind')!r} <> registry {spec.basis_kind!r}")
        ev_rows = _rows(view, _same_run(view, df, spec.evidence_family)) if spec.evidence else None
        ev_ids = {e["evidence_id"] for e in ev_rows or []}
        for r in rows:
            at = f"{name}: {r['doc_id']}/{r['item_key']!r}"
            if r["basis_kind"] != spec.basis_kind:
                bad.append(f"{at}: basis_kind {r['basis_kind']!r} <> registry {spec.basis_kind!r}")
            if r["evidence_id"] is not None:
                if not spec.evidence:
                    bad.append(f"{at}: a pure workflow's row names evidence {r['evidence_id']}")
                elif r["evidence_id"] not in ev_ids:
                    bad.append(f"{at}: evidence_id {r['evidence_id']} not in the run's {spec.evidence_family} file")
            elif spec.evidence and r["status"] in OK_LIKE and r["origin"] != "human" and not r["override_ids"]:
                bad.append(f"{at}: {r['status']} ({r['origin']}) without evidence or override_ids")
            if r["origin"] == "human" and not r["override_ids"]:
                bad.append(f"{at}: origin human without override_ids")
    return _finding("V-wf", "{n} <wf> rows break V7", bad)


# ---------------------------------------------------------------- V8
@register("V8", "V-evidence", scope="store")
def v_evidence(view: StoreView) -> list[Finding]:
    """V8 (module docstring)."""
    bad: list[str] = []
    specs = {**_annotate_workflows(), "triage": TRIAGE_EVIDENCE_SPEC}
    for df in view.files:
        if df.spec is None or df.spec.kind != "evidence" or df.spec.workflow not in specs or not view.in_scope(df):
            continue
        spec = specs[df.spec.workflow]
        rows = _rows(view, df)
        if rows is None:
            continue
        name = df.key
        # An evidence file written by ``stack migrate upcast`` (stage ``migrate.upcast``) is a copy of an
        # older run's evidence in the current schema: it has no same-run <wf> file (digests still recompute).
        if (spec.workflow not in CHAIN_WORKFLOWS and df.header.get("stage") != "migrate.upcast"
                and _same_run(view, df, spec.workflow) is None):
            bad.append(f"{name}: no {spec.workflow} file of the same run")
        for r in rows:
            at = f"{name}: {r['evidence_id']}"
            want = with_digests(r, spec)
            for col in ("work_digest", "result_digest"):
                if r[col] != want[col]:
                    bad.append(f"{at}: {col} {r[col]} does not recompute ({want[col]})")
            if r["basis_kind"] != spec.basis_kind:
                bad.append(f"{at}: basis_kind {r['basis_kind']!r} <> registry {spec.basis_kind!r}")
            if r["item_key"] != "":
                bad.append(f"{at}: item_key {r['item_key']!r} (an invocation is per document)")
            if r["origin"] not in ANN_ORIGINS:
                bad.append(f"{at}: origin {r['origin']!r} not in {list(ANN_ORIGINS)}")
    for df, spec in _wf_files(view):
        if view.in_scope(df) and spec.evidence and _same_run(view, df, spec.evidence_family) is None:
            bad.append(f"{df.key}: evidence: true but no {spec.evidence_family} file of the same run")
    return _finding("V-evidence", "{n} evidence problems (V8)", bad)


# ---------------------------------------------------------------- X scope
@dataclass(frozen=True)
class _Run:
    spec: PayloadSpec
    wf: DataFile
    rows: list[dict[str, Any]]
    evidence: list[dict[str, Any]] | None
    corpus: dict[str, dict[str, Any]] | None
    problems: tuple[str, ...]


def _finished(df: DataFile) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(df.header.get("finished_at", ""))
    except ValueError:
        return dt.datetime.min.replace(tzinfo=dt.UTC)


def _inputs(df: DataFile) -> dict[str, Any]:
    try:
        ins = json.loads(df.header.get("inputs") or "{}")
    except ValueError:
        return {}
    return ins if isinstance(ins, dict) else {}


def _scope_files(view: StoreView) -> tuple[list[tuple[DataFile, PayloadSpec]], list[str]]:
    wfs = _annotate_workflows()
    problems: list[str] = []
    if view.build is not None:
        out = []
        for key, entry in sorted((view.build.get("files") or {}).items()):
            if key not in wfs:
                continue
            fid = (entry or {}).get("file_id")
            df = view.file(fid) if fid else None
            if df is None:
                problems.append(f"build {view.build.get('build_id')}: {key} file {fid} not under data/")
            elif view.selected(df):
                out.append((df, wfs[key]))
        return out, problems
    newest: dict[str, DataFile] = {}
    for df, _spec in _wf_files(view):
        if "corpus" not in _inputs(df):  # not an annotate.<wf> run's output (it always names one)
            continue
        cur = newest.get(df.family or "")
        if cur is None or (_finished(df), df.file_id) > (_finished(cur), cur.file_id):
            newest[df.family or ""] = df
    return [(df, wfs[w]) for w, df in sorted(newest.items())], problems


#: What X7 and X8 read of a ``corpus`` row: the fields :func:`stack.annotate.basis.basis_of`,
#: :func:`~stack.annotate.basis.live_members` and :func:`~stack.annotate.basis.is_basis_current`
#: look at (``title``/``abstract`` only for whether a ``fields`` basis exists), projected out of
#: their blocks (:func:`stack.store.parquet.read_family_fields`). A run that joins reads, or whose
#: workflow has a ``chained`` basis, also gets the whole ``annotations`` block (the join rebuilds
#: it, the chained basis reads it). Anything else those functions read must be added here: a
#: narrowed row has no other keys.
CORPUS_FIELDS: tuple[str, ...] = (
    "doc_id",
    "identity.anchor_source_id", "identity.members.source_id", "identity.members.license_class",
    "identity.members.is_live",
    "metadata.title", "metadata.abstract", "metadata.fields_digest",
    "content.current_text_sha256", "content.current_source_id",
)


def _joined_corpus(view: StoreView, df: DataFile, corpus_id: str, mine: list[str], spec: PayloadSpec,
                   cache: dict[tuple[Any, ...], dict[str, dict[str, Any]]]) -> dict[str, dict[str, Any]] | None:
    """The corpus rows ``df``'s run saw, by ``doc_id``: its ``corpus`` input (:data:`CORPUS_FIELDS`)
    joined with the header's reads (module docstring); None (and a problem in ``mine``) when a
    file is not readable. ``cache`` (one per check) holds each ``(corpus, reads, fields)`` once,
    so the runs over one corpus share it."""
    from stack.annotate.inputs import header_reads, join_reads

    reads_ids = header_reads(_inputs(df))
    fields = CORPUS_FIELDS + (("annotations",) if reads_ids or spec.reads else ())
    key = (corpus_id, tuple(reads_ids.items()), fields)
    if key in cache:
        return cache[key]
    c_df = view.file(corpus_id)
    tbl = view.read(c_df, fields) if c_df is not None else None
    if tbl is None:
        mine.append(f"{df.key}: corpus input {corpus_id} not readable under data/")
        return None
    reads = {}
    for name, fid in reads_ids.items():
        r_df = view.file(fid)
        r_tbl = view.table(r_df) if r_df is not None else None
        if r_tbl is None:
            mine.append(f"{df.key}: read input {name}={fid} not readable under data/")
            return None
        reads[name] = r_tbl
    joined = join_reads(tbl, reads)
    out: dict[str, dict[str, Any]] = {}
    for start in range(0, joined.num_rows, BATCH_ROWS):
        for r in joined.slice(start, BATCH_ROWS).to_pylist():
            out[r["doc_id"]] = r
    cache[key] = out
    return out


def _annotate_runs(view: StoreView, *, corpus: bool = True) -> tuple[Iterator[_Run], list[str]]:
    """The X-scope runs (module docstring), one at a time: a run's rows are dropped before the
    next run's are read. The evidence file of several records (one producer run) and the
    narrowed corpus they share are read once. ``corpus=False`` (X6) reads no corpus."""
    files, problems = _scope_files(view)

    def read(df: DataFile) -> list[dict[str, Any]] | None:  # uncached: each file is read once here
        tbl = view.read(df)
        return None if tbl is None else tbl.to_pylist()

    def runs() -> Iterator[_Run]:
        corpora: dict[tuple[Any, ...], dict[str, dict[str, Any]]] = {}
        evidence: dict[str, list[dict[str, Any]] | None] = {}
        for df, spec in files:
            rows = read(df)
            if rows is None:
                continue
            mine: list[str] = []
            ev = None
            if spec.evidence:
                ev_df = _same_run(view, df, spec.evidence_family)
                if ev_df is not None and ev_df.file_id not in evidence:
                    evidence.clear()  # the last one only: the records of one producer run come in a row
                    evidence[ev_df.file_id] = read(ev_df)
                ev = evidence[ev_df.file_id] if ev_df is not None else None
                if ev is None:
                    mine.append(f"{df.key}: no readable {spec.evidence_family} file of the same run")
            corpus_id = _inputs(df).get("corpus")
            c_rows = None
            if corpus_id is None:
                mine.append(f"{df.key}: header inputs name no corpus file")
            elif corpus:
                c_rows = _joined_corpus(view, df, corpus_id, mine, spec, corpora)
            yield _Run(spec, df, rows, ev, c_rows, tuple(mine))
            del rows, ev, c_rows

    return runs(), problems


# ---------------------------------------------------------------- accepted configs (X6, X8)
def _applied_from(view: StoreView, run_id: str | None, stage: str, cfg: str | None) -> tuple[tuple[str, ...], list[str]]:
    """The accepted configs the run ``run_id`` applied (its record's ``selection``, D758; module docstring, Accepted
    rows) and the problems: a selection whose ``target`` is not the header's config, or whose digest does not
    recompute from its own target and list. A run that recorded none: ``()``."""
    from stack.plan import selection_digest
    from stack.runs import find_run_record

    rec = find_run_record(view.root, run_id) if run_id else None
    sel = rec.selection if rec is not None else None
    if not sel:
        return (), []
    where = f"run {run_id} ({stage})"
    acc = [a for a in sel.get("accepted") or [] if a.get("question") is None]
    problems = []
    if sel.get("target") != cfg:
        problems.append(f"{where}: selection target {_short(str(sel.get('target')))} is not the header's config "
                        f"{_short(cfg)}")
    if sel.get("digest") != selection_digest(sel.get("target") or "", sel.get("accepted") or []):
        problems.append(f"{where}: selection digest {_short(str(sel.get('digest')))} does not recompute")
    return tuple(str(a["config"]) for a in acc), problems


def _same_basis_from(view: StoreView, run_id: str | None) -> bool:
    """Whether the run ``run_id`` selected same-basis answers of another supplier (its record's ``selection``
    says ``same_basis: true``, D807); a run recorded before D807 did not."""
    from stack.runs import find_run_record

    rec = find_run_record(view.root, run_id) if run_id else None
    return bool(rec is not None and rec.selection and rec.selection.get("same_basis") is True)


def _own_pins(view: StoreView, fname: str, spec: PayloadSpec, rows: list[dict[str, Any]]) -> list[str]:
    """X6 for accepted ``ai_check`` rows: each judged against the pin its own run recorded (module docstring)."""
    bad: list[str] = []
    by_run: dict[str, list[dict[str, Any]]] = {}
    for e in rows:
        by_run.setdefault(str(e["run_id"]), []).append(e)
    for run_id, evs in sorted(by_run.items()):
        pin, why = _recorded_pin(view, run_id)
        if pin is None:
            bad.append(f"{fname}: {len(evs)} accepted evidence rows of run {run_id}: {why}")
            continue
        if spec.is_record and spec.workflow not in pin.made:
            bad.append(f"{fname}: accepted evidence of run {run_id}, whose pin does not produce {spec.workflow}")
        for e in evs:
            at = f"{fname}: accepted evidence {e['evidence_id']} (run {run_id})"
            if pin.digest != e["config_digest"]:
                bad.append(f"{at}: its run's prompt_pin hashes to {_short(pin.digest)}, not its config_digest "
                           f"{_short(e['config_digest'])}")
            if pin.name is not None and e["prompt_name"] != pin.name:
                bad.append(f"{at}: prompt_name {e['prompt_name']!r} <> its run's pin {pin.name!r}")
            if e["template_digest"] not in pin.templates:
                bad.append(f"{at}: template_digest {e['template_digest']} not its run's pinned template's")
    return bad


# ---------------------------------------------------------------- X6
@dataclass(frozen=True)
class _Pin:
    """A pin X6 judges by: prompt name, template digests, produced records, config digest."""

    name: str | None
    templates: frozenset[str]
    made: tuple[str, ...]
    digest: str | None


def _current_pin(plan_name: str, workflow: str) -> tuple[_Pin | None, list[str]]:
    """Today's pin of ``annotate.<workflow>`` under the plan (``config_digest`` recomputed as the
    stage does), or ``(None, problems)``."""
    from stack.annotate.stage import config_digest, resolve_pin, stack_config
    from stack.build import find_plan
    from stack.plan import PlanError, load_plan
    from stack.stage_runner import load_stage_module

    try:
        plan = load_plan(find_plan(plan_name))
    except PlanError as exc:
        return None, [f"plan {plan_name!r} not loadable: {exc}"]
    plan_pin = plan.prompts.get(f"annotate.{workflow}", {})
    try:
        module = load_stage_module(f"annotate.{workflow}")
        wf = module.WORKFLOW
        pin = resolve_pin(wf, plan_pin, stack_config)
        made = wf.produced(plan_pin)
        cfg = config_digest(wf.spec, plan_pin, pin)
    except Exception as exc:  # noqa: BLE001 - any load failure is the finding, never a pass
        return None, [f"annotate.{workflow}: pin not recomputable ({type(exc).__name__}: {exc})"]
    return _Pin(plan_pin.get("name"), frozenset(pin.template_digests), tuple(made), cfg), []


def _recorded_pin(view: StoreView, run_id: str | None) -> tuple[_Pin | None, str | None]:
    """The run record's ``prompt_pin`` as a :class:`_Pin` (``digest`` = the hash of its digest
    keys), or ``(None, why)`` when the run recorded none."""
    from stack.annotate.stage import PIN_DIGEST_KEYS
    from stack.checks.digests import H
    from stack.runs import find_run_record

    rec = find_run_record(view.root, run_id) if run_id else None
    if rec is None:
        return None, f"no run record for {run_id}"
    p = rec.prompt_pin
    if not p:
        return None, f"run {run_id} recorded no prompt_pin (written before R1-1)"
    try:
        digest = H({k: p[k] for k in PIN_DIGEST_KEYS})
    except KeyError as exc:
        return None, f"run {run_id}: prompt_pin lacks {exc}"
    return _Pin(p.get("prompt_name"), frozenset(p.get("template_digests") or ()),
                tuple(p.get("produced") or ()), digest), None


def _short(d: str | None) -> str:
    return f"{d[:12]}…" if d else "-"


@register("X6", "policy_pins", scope="store")
def policy_pins(view: StoreView) -> list[Finding]:
    """X6 (module docstring)."""
    runs, _ = _annotate_runs(view, corpus=False)
    bad: list[str] = []
    unproduced: list[str] = []
    unpinned: list[str] = []
    moved: list[str] = []
    for run in runs:
        if run.evidence is None:
            continue
        chosen = {r["evidence_id"] for r in run.rows if r["evidence_id"] is not None}
        own = run.wf.header.get("run_id")
        fname, header_cfg = run.wf.key, run.wf.header.get("config_digest")
        producer = run.spec.produced_by
        foreign = [e for e in run.evidence if e["evidence_id"] in chosen and e["config_digest"] != header_cfg]
        if foreign:  # D758: accepted rows, each judged by its own run's pin
            accepted, problems = _applied_from(view, own, f"annotate.{producer}", header_cfg)
            bad += [f"{fname}: {p}" for p in problems]
            bad += [f"{fname}: selected evidence {e['evidence_id']} of config {_short(e['config_digest'])} is not "
                    f"among the run's accepted configs {[_short(d) for d in accepted]}"
                    for e in foreign if e["config_digest"] not in accepted]
            bad += _own_pins(view, fname, run.spec, [e for e in foreign if e["origin"] == "ai_check"
                                                     and e["status"] in OK_LIKE])
        rows = [e for e in run.evidence if e["origin"] == "ai_check" and e["status"] in OK_LIKE
                and (e["evidence_id"] in chosen or e["run_id"] == own) and e["config_digest"] == header_cfg]
        if not rows:
            continue
        current, problems = _current_pin(run.wf.header.get("plan", ""), producer)
        pin, why = _recorded_pin(view, own)
        if pin is not None:
            if pin.digest != header_cfg:
                bad.append(f"{fname}: the run record's prompt_pin hashes to {_short(pin.digest)}, not the header's "
                           f"config_digest {_short(header_cfg)}")
            if problems:
                moved += [f"{fname}: cannot compare with the plan's current pin: {p}" for p in problems]
            elif current is not None and current.digest != pin.digest:
                moved.append(f"{fname}: the plan's current annotate.{producer} pin differs from the one this "
                             f"build's run asked under (config_digest {_short(pin.digest)} -> "
                             f"{_short(current.digest)}; prompt {pin.name!r} -> {current.name!r}): the next build "
                             f"re-asks")
        else:
            bad += [f"{fname}: {p}" for p in problems]
            if current is not None and current.digest == header_cfg:
                pin = current  # today's pin is the run's: the header digest says so
            elif current is not None:
                off = sum(1 for e in rows if (current.name is not None and e["prompt_name"] != current.name)
                          or e["template_digest"] not in current.templates)
                unpinned.append(f"{fname}: {why}, and the plan's current annotate.{producer} pin is not the "
                                f"run's (config_digest {_short(header_cfg)} -> {_short(current.digest)}): "
                                f"{len(rows)} evidence rows not checked against a pin ({off} of them off "
                                f"today's prompt name or templates)")
        if pin is None:
            continue
        if run.spec.is_record and run.spec.workflow not in pin.made:
            unproduced.append(f"{fname}: record {run.spec.workflow} written, but the run's "
                              f"annotate.{producer} pin produces {list(pin.made)}")
        for e in rows:
            at = f"{fname}: evidence {e['evidence_id']}"
            if pin.name is not None and e["prompt_name"] != pin.name:
                bad.append(f"{at}: prompt_name {e['prompt_name']!r} <> the run's pin {pin.name!r}")
            if e["template_digest"] not in pin.templates:
                bad.append(f"{at}: template_digest {e['template_digest']} not the run's pinned template's")
    return (_finding("policy_pins", "{n} record file(s) the run's producer pin does not produce (X6)", unproduced)
            + _finding("policy_pins", "{n} evidence rows off the run's prompt pin (X6)", bad)
            + _finding("policy_pins", "{n} run(s) without a recorded pin whose plan pin changed since (X6)",
                       unpinned, "warn")
            + _finding("policy_pins", "{n} file(s) whose plan pin changed since their run (X6)", moved, "warn"))


# ---------------------------------------------------------------- X7
@register("X7", "basis_currency", scope="store")
def basis_currency(view: StoreView) -> list[Finding]:
    """X7 (module docstring)."""
    runs, problems = _annotate_runs(view)
    bad = list(problems)
    for run in runs:
        if run.corpus is None:
            bad += [p for p in run.problems if "corpus" in p]
            continue
        for r in run.rows:
            at = f"{run.wf.key}: {r['doc_id']}/{r['item_key']!r}"
            d = run.corpus.get(r["doc_id"])
            if d is None:
                bad.append(f"{at}: document not in the corpus input")
                continue
            want = is_basis_current(r["basis_kind"], r["basis_digest"], d, r["workflow"])
            if r["is_basis_current"] != want:
                bad.append(f"{at}: is_basis_current {r['is_basis_current']} <> recomputed {want}")
    return _finding("basis_currency", "{n} is_basis_current values do not recompute (X7)", bad)


# ---------------------------------------------------------------- X8
def _diff(a: dict[str, Any], b: dict[str, Any], skip: frozenset[str]) -> list[str]:
    return sorted(k for k in a.keys() | b.keys() if k not in skip and a.get(k) != b.get(k))


@register("X8", "selection_recompute", scope="store")
def selection_recompute(view: StoreView) -> list[Finding]:
    """X8 (module docstring)."""
    from stack.annotate.select import RecordMissing, select
    from stack.overrides import FAMILY as OVERRIDES
    from stack.overrides import active, rows_of

    runs, problems = _annotate_runs(view)
    bad = list(problems)
    for run in runs:
        if not run.spec.evidence:
            continue
        if run.problems:
            bad += list(run.problems)
            continue
        assert run.corpus is not None and run.evidence is not None
        name = run.wf.key
        docs_ids = sorted({r["doc_id"] for r in run.rows})
        missing = [d for d in docs_ids if d not in run.corpus]
        if missing:
            bad += [f"{name}: {d}: document not in the corpus input" for d in missing]
            continue
        ov_id = _inputs(run.wf).get(OVERRIDES)
        ov = view.view_at(str(ov_id)) if ov_id is not None else None
        if ov_id is not None and ov is None:
            bad.append(f"{name}: the pinned overrides view at {ov_id} does not read")
            continue
        entries, _ = active(rows_of(ov) if ov is not None else [], run.spec.workflow)
        unreached = {r["doc_id"]: (r["status"], r["error_class"]) for r in run.rows
                     if r["evidence_id"] is None and r["status"] == "not_attempted"}
        accepted, problems = _applied_from(view, run.wf.header.get("run_id"), f"annotate.{run.spec.produced_by}",
                                           run.wf.header.get("config_digest"))
        bad += [f"{name}: {p}" for p in problems]
        try:
            got = select(run.spec, [run.corpus[d] for d in docs_ids], run.evidence, entries,
                         config_digest=run.wf.header.get("config_digest", ""),
                         run_id=run.wf.header.get("run_id", ""), now=dt.datetime.now(dt.UTC),
                         unreached=unreached, accepted=accepted,
                         same_basis=_same_basis_from(view, run.wf.header.get("run_id")))
        except RecordMissing as exc:
            bad.append(f"{name}: {exc}")
            continue
        have = {(r["doc_id"], r["item_key"]): r for r in run.rows}
        want = {(r["doc_id"], r["item_key"]): r for r in got}
        for key in sorted(have.keys() ^ want.keys()):
            side = "file only" if key in have else "recomputed only"
            bad.append(f"{name}: {key[0]}/{key[1]!r}: {side}")
        for key in sorted(have.keys() & want.keys()):
            h, w = have[key], want[key]
            skip = frozenset({"is_basis_current"}) | (_STAMPS if w["evidence_id"] is None else frozenset())
            cols = _diff(h, w, skip)
            if cols:
                bad.append(f"{name}: {key[0]}/{key[1]!r}: differs in {cols}")
    return _finding("selection_recompute", "{n} <wf> rows select does not reproduce (X8)", bad)


__all__ = ["basis_currency", "policy_pins", "selection_recompute", "v_evidence", "v_wf"]
