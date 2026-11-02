"""The triage checks (Appendix B; T2.9): V5 ``V-annotations`` (per file) and X13 ``gate_obeyed``,
X14 ``gate_union``, X19 ``state_monotone``, X20 ``triage_current`` (store scope).

* **V5** (``documents``/``corpus`` files): every typed annotation present names its own
  ``workflow``, the registry's ``version`` and ``basis_kind``; ``status`` and ``origin`` from their
  vocabularies; an ``ok`` annotation has a payload and a ``basis_digest``; ``other[]`` entries'
  ``payload_json`` parses; in ``documents`` only ``triage`` is non-NULL and ``other[]`` is empty
  (§2.4); a triage payload pairs ``decision`` with ``status`` (``fetch``/``skip`` ⇔ ``ok``,
  ``pending`` ⇔ not ``ok``), has ``wanted_by`` non-empty ⇔ ``fetch``, and lists each question once.
* **X13** (D20): in each ``acquire`` file of a ``documents`` chain, every role the run attempted
  (it records an attempt in this file: new against the file's base view with ``last_attempt_at``
  set or ``attempts > 0``, or existing with ``attempts``/``last_attempt_at`` changed; or it points
  at a raw snapshot new in this file, a fetch that minted) that is not in-band
  (``lane = 'inband'``, or its snapshot ``via``/``kind`` is ``inband``) belongs to a row whose
  triage (as carried in that file) is ``fetch``, or ``skipped`` with ``no_basis`` or
  ``gate_failed``. A new role with no attempt recorded (``carry``'s ``not_attempted`` or
  ``skipped[gated|withdrawn|not_needed]``: ``attempts = 0``, ``last_attempt_at`` NULL, no new
  snapshot) is not an attempt (T3.6-f4: the first acquire over a triage-``skip`` row writes its
  ``skipped[gated]`` roles, D108). Per file against its base is "within the build's runs" without a
  build.
* **X14**: over the reference build's ``documents`` head view, every unmasked, live row in state
  ``triaged``/``acquired`` recomputes (:func:`stack.gate.recompute`) its ``decision``, ``status``,
  ``error_class`` and ``wanted_by`` under the build's gate: the blob named by the
  build record's ``gate_digest`` (the triage stage stores it, :func:`stack.gate.store_gate`), else
  the plan file named by the record's ``plan`` (:func:`stack.gate.resolve_plan`) when the record
  has no ``gate_digest`` or the file has the same; ``skipped[no_builds]`` without a build,
  ``skipped[no_gate]`` when neither gives the gate (a world whose triage never ran for real).
  Arm 7 (the carried standing, T5.5): when the recomputed arm is 6, the stored decision is an
  ``ok`` ``fetch``/``skip``, every stored question row is a re-derived fill
  (``skipped[not_needed|not_requested]``) and the row's primary role is ``ok`` (``wants`` from
  the plan the record names, :func:`stack.gate.resolve_plan`, with an ``ok`` arXiv ``html`` role
  counting for ``full_text`` when that plan's acquire line sets ``arxiv_pdf: on_miss | never``,
  D485; no plan resolves: that test is skipped), the stored decision is its own standing and recomputes as arm 7 (``wanted_by``
  filtered to the gate's consumers), so a carried row agrees and a primary-role-not-``ok`` row
  edited to ``fetch`` does not. The primary role counts only the roles of the row's current members
  (:func:`stack.gate.primary_role_ok`). On a row in the question scope (no ``sql:`` hit, primary role
  not ``ok``; a plan resolves) a stored signal counts as the ``ok`` answer it records, as triage
  carries it there (D785: a row triage decided outside the question scope whose primary role is no
  longer ``ok`` recomputes to what triage would decide now, not to arm 6).
  The rows reach Python and the recompute :data:`BATCH_ROWS` at a time (S25-54): the recompute is
  per row (a ``sql:`` clause is a predicate on one gate-view row,
  as the triage stage, which evaluates it over its scoped rows only, already assumes), so the
  findings are those of one whole-view pass.
* **X19**: per ``documents`` chain file and its base view, every row's state move is allowed by
  :func:`stack.states.is_forward` (``digest_changed`` = ``members_digest`` or ``fields_digest``
  differ; ``separate`` = the file is ``gather``'s; ``→ retired`` only in a ``gather`` file).
* **X20**: in each ``documents`` chain's head view, every unmasked, not-``retired`` row is
  ``identified`` or carries a triage annotation (a missing one is S8's) with ``basis_kind = 'fields'``, ``basis_digest =
  metadata.fields_digest`` and ``is_basis_current`` not false. (S9, as T1.4 implemented it, already
  holds the digest agreement per file for ``triaged`` and ``acquired``; X20 states it over the view
  and adds the annotation's own currency flag.)
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc

from stack import gate
from stack.checks import Finding, register
from stack.checks.chain import BATCH_ROWS, SkipCheck, StoreView, base_gap
from stack.checks.rows import iter_rows, row_slices
from stack.plan import Gate, PlanError
from stack.schemas.families import FamilySpec
from stack.schemas.payloads.registry import REGISTRY as PAYLOADS
from stack.schemas.vocab import ANN_ORIGINS, STATUS, TRIAGE_DECISIONS
from stack.states import is_forward
from stack.store import chain as store_chain
from stack.store.blobs import open_blobs

Header = dict[str, str]
_EXAMPLES = 5
_ROW_FAMILIES = ("documents", "corpus")
#: Triage statuses under which ``acquire`` may fetch (§3.1).
FETCHABLE_SKIPS = ("no_basis", "gate_failed")


def _finding(check: str, message: str, bad: list[str]) -> list[Finding]:
    return [Finding(check, "hard", message.format(n=len(bad)), len(bad), tuple(bad[:_EXAMPLES]))] \
        if bad else []


# ---------------------------------------------------------------- V5
def _ann_problems(d: str, wf: str, a: dict[str, Any]) -> list[str]:
    spec = PAYLOADS.get(wf)
    out: list[str] = []
    if a["workflow"] != wf:
        out.append(f"{d}: annotations.{wf}.workflow = {a['workflow']!r}")
    if spec is not None and a["version"] != spec.version:
        out.append(f"{d}: annotations.{wf}.version {a['version']} <> registry {spec.version}")
    if spec is not None and a["basis_kind"] != spec.basis_kind:
        out.append(f"{d}: annotations.{wf}.basis_kind {a['basis_kind']!r} <> registry {spec.basis_kind!r}")
    if a["status"] not in STATUS:
        out.append(f"{d}: annotations.{wf}.status {a['status']!r}")
    if a["origin"] not in ANN_ORIGINS:
        out.append(f"{d}: annotations.{wf}.origin {a['origin']!r}")
    if a["status"] == "ok" and (a["payload"] is None or a["basis_digest"] is None):
        out.append(f"{d}: annotations.{wf} ok without payload or basis_digest")
    return out


def _triage_problems(d: str, a: dict[str, Any]) -> list[str]:
    p = a["payload"]
    if p is None:
        return [f"{d}: triage without a payload"]
    out: list[str] = []
    dec, st = p["decision"], a["status"]
    if dec not in TRIAGE_DECISIONS:
        out.append(f"{d}: triage decision {dec!r}")
    elif (dec in ("fetch", "skip")) != (st == "ok"):
        out.append(f"{d}: triage decision {dec!r} with status {st!r}")
    if bool(p["wanted_by"]) != (dec == "fetch"):
        out.append(f"{d}: triage decision {dec!r} with wanted_by {p['wanted_by']}")
    names = [q["question"] for q in p["questions"] or []]
    if len(names) != len(set(names)):
        out.append(f"{d}: triage lists a question twice: {names}")
    return out


@register("V5", "V-annotations")
def v_annotations(table: pa.Table, header: Header, spec: FamilySpec) -> list[Finding]:
    """V5 (module docstring)."""
    if spec.kind not in _ROW_FAMILIES:
        return []
    bad: list[str] = []
    for r in iter_rows(table, ["doc_id", "annotations"]):
        d, anns = r["doc_id"], r["annotations"]
        for wf, a in anns.items():
            if wf == "other" or a is None:
                continue
            bad += _ann_problems(d, wf, a)
            if wf == gate.WORKFLOW:
                bad += _triage_problems(d, a)
            elif spec.kind == "documents":
                bad.append(f"{d}: {wf} in documents (only triage, §2.4)")
        for o in anns["other"] or []:
            if spec.kind == "documents":
                bad.append(f"{d}: other[] {o['workflow']} in documents (only triage, §2.4)")
            try:
                json.loads(o["payload"]["payload_json"])
            except (TypeError, ValueError):
                bad.append(f"{d}: other[] {o['workflow']} payload_json does not parse")
    return _finding("V-annotations", f"{spec.name}: {{n}} annotation problem(s)", bad)


# ---------------------------------------------------------------- X13
def _fetchable(row: dict[str, Any]) -> bool:
    t = gate.triage_of(row)
    if t is None or t["payload"] is None:
        return False
    return t["payload"]["decision"] == "fetch" or (
        t["status"] == "skipped" and t["error_class"] in FETCHABLE_SKIPS)


def _attempted_roles(cur: dict[str, Any], prev: dict[str, Any] | None) -> list[str]:
    c = cur["content"]
    if c is None:
        return []
    pc = (prev or {}).get("content") or {}
    before = {(r["source_id"], r["content_role"]): r for r in pc.get("roles") or []}
    snaps_before = {s["snapshot_id"] for s in pc.get("snapshots") or []}
    snaps = {s["snapshot_id"]: s for s in c["snapshots"] or []}
    out: list[str] = []
    for r in c["roles"] or []:
        snap = snaps.get(r["snapshot_id"]) if r["snapshot_id"] is not None else None
        if r.get("lane") == "inband" or (snap is not None and "inband" in (snap["via"], snap["kind"])):
            continue
        p = before.get((r["source_id"], r["content_role"]))
        recorded = ((r["last_attempt_at"] is not None or (r["attempts"] or 0) > 0) if p is None
                    else (p["attempts"], p["last_attempt_at"]) != (r["attempts"], r["last_attempt_at"]))
        if recorded or (snap is not None and snap["snapshot_id"] not in snaps_before):
            out.append(f"{r['content_role']}/{r['source_id']}")
    return out


#: What X13 reads (projected, :meth:`StoreView.read`): of ``content``, what
#: :func:`_attempted_roles` compares; of ``annotations``, what :func:`_fetchable` and the message read.
X13_CONTENT: tuple[str, ...] = (
    "content.roles.source_id", "content.roles.content_role", "content.roles.snapshot_id", "content.roles.lane",
    "content.roles.attempts", "content.roles.last_attempt_at",
    "content.snapshots.snapshot_id", "content.snapshots.via", "content.snapshots.kind",
)
X13_TRIAGE: tuple[str, ...] = (
    "annotations.triage.status", "annotations.triage.error_class", "annotations.triage.payload.decision",
)


@register("X13", "gate_obeyed", scope="store")
def gate_obeyed(view: StoreView) -> list[Finding]:
    """X13 (module docstring)."""
    bad: list[str] = []
    for df in view.pair_files("X13"):
        if df.header.get("stage") != "acquire":
            continue
        if not view.readable(df):
            continue
        if not view.has_base(df):
            base_gap(view, df, bad)
            continue
        # _attempted_roles reads content; _fetchable and the message the triage annotation
        tbl = view.read(df, ["doc_id", *X13_CONTENT, *X13_TRIAGE])
        assert tbl is not None
        base = view.base_view(df, ["doc_id", *X13_CONTENT], keys=tbl.column("doc_id").unique())
        if base is None:
            base_gap(view, df, bad)
            continue
        pos = {d: i for i, d in enumerate(base.column("doc_id").to_pylist())}
        for start in range(0, tbl.num_rows, BATCH_ROWS):
            rows = tbl.slice(start, BATCH_ROWS).to_pylist()
            hit = [r["doc_id"] for r in rows if r["doc_id"] in pos]
            prev = dict(zip(hit, base.take([pos[d] for d in hit]).to_pylist(), strict=True))
            for r in rows:
                roles = _attempted_roles(r, prev.get(r["doc_id"]))
                if roles and not _fetchable(r):
                    t = gate.triage_of(r)
                    what = "no triage" if t is None else (
                        f"{(t['payload'] or {}).get('decision')} {t['status']}[{t['error_class']}]")
                    bad.append(f"{df.file_id}: {r['doc_id']} fetched {roles[:2]} under {what}")
    return _finding("gate_obeyed", "{n} fetch(es) outside the gate", bad)


# ---------------------------------------------------------------- X14
def _build_heads(view: StoreView) -> list[str]:
    return [fid for fid in view.build_heads().values()
            if (df := view.file(fid)) is not None and df.spec is not None and df.spec.kind == "documents"]


def _reference_gate(view: StoreView) -> Gate:
    """The build's gate: the blob named by the build's ``gate_digest`` (written by
    the triage stage), else the plan file the build names when it records no ``gate_digest`` or
    the same one; ``SkipCheck('no_gate')`` otherwise."""
    assert view.build is not None
    want = view.build.get("gate_digest")
    if want:
        found = gate.load_gate(open_blobs(view.root), str(want))
        if found is not None:
            return found
    plan = gate.resolve_plan(str(view.build.get("plan") or ""))
    if plan is not None and (not want or want == plan.gate_digest):
        return plan.gate
    raise SkipCheck("no_gate")


@register("X14", "gate_union", scope="store")
def gate_union(view: StoreView) -> list[Finding]:
    """X14 (module docstring)."""
    heads = _build_heads(view)  # SkipCheck('no_builds')
    gate_ = _reference_gate(view)
    assert view.build is not None
    try:
        plan = gate.resolve_plan(str(view.build.get("plan") or ""))
    except PlanError:
        plan = None  # the gate came from the derived blob; arm 7's primary-role test is skipped
    wants = plan.acquire.wants if plan is not None else None
    html_primary = gate.html_is_primary(plan)  # D485, as triage decided
    groups = gate.channel_groups()
    bad: list[str] = []
    for fid in heads:
        tbl = view.view_at(fid)  # whole rows: recompute rebuilds documents rows for the gate's SQL
        if tbl is None:
            continue
        masked = pc.fill_null(pc.struct_field(tbl.column("identity"), "is_masked"), False)
        keep = pc.and_(pc.is_in(tbl.column("state"), value_set=pa.array(["triaged", "acquired"])),
                       pc.invert(masked))
        # S25-54: BATCH_ROWS rows at a time; per doc_id the first row's stored triage, the last
        # row's recompute (as one whole-view pass had it)
        first: dict[str, tuple[Any, ...] | None] = {}
        decisions: dict[str, gate.Decision] = {}
        for _, part in row_slices(tbl.filter(keep)):
            rows = part.to_pylist()
            for r in rows:
                if r["doc_id"] not in first:
                    t = gate.triage_of(r)
                    first[r["doc_id"]] = None if t is None else (
                        t["payload"]["decision"], t["status"], t["error_class"], tuple(t["payload"]["wanted_by"]))
            decisions.update(gate.recompute(rows, gate_, groups, carry=True, wants=wants, html_primary=html_primary))
        for d, dec in sorted(decisions.items()):
            have = first[d]
            assert have is not None
            got = (dec.decision, dec.status, dec.error_class, dec.wanted_by)
            if have != got:
                bad.append(f"{fid}: {d} triage {have} <> recomputed {got}")
    return _finding("gate_union", "{n} triage decision(s) do not recompute under the gate", bad)


# ---------------------------------------------------------------- X19
#: What X19 reads of a file and its base view: the state and the two digests a move may depend
#: on, projected out of their blocks (:meth:`StoreView.base_join`), never the blocks themselves.
X19_FIELDS: tuple[str, ...] = ("state", "identity.members_digest", "metadata.fields_digest")


def _differs(a: pa.ChunkedArray, b: pa.ChunkedArray) -> pa.ChunkedArray:
    """Python's ``a != b`` per row, NULL as ``None``: NULL against a value differs, two NULLs do not."""
    return pc.fill_null(pc.not_equal(a, b), pc.xor(pc.is_null(a), pc.is_null(b)))


def _digest(tbl: pa.Table, block: str, name: str, fid: str) -> pa.ChunkedArray:
    col = tbl.column(block)
    if col.null_count:  # the row-wise check raised on a NULL block; so does this one (a check_error)
        raise TypeError(f"{fid}: {col.null_count} row(s) with a NULL {block} block")
    return pc.struct_field(col, name)


def _move_ok(a: str | None, b: str | None, changed: bool, stage: str | None) -> bool:
    try:
        ok = is_forward(a, b, digest_changed=changed, separate=stage == "gather")  # type: ignore[arg-type]
    except KeyError:
        ok = False  # V3 names the unknown state
    if ok and b == "retired" and a != b and stage != "gather":
        ok = False
    return ok


@register("X19", "state_monotone", scope="store")
def state_monotone(view: StoreView) -> list[Finding]:
    """X19 (module docstring). Per file, the file and its base view joined on ``doc_id`` with
    only :data:`X19_FIELDS` read; the digest comparison is columnar and :func:`is_forward` is
    asked once per distinct ``(from, to, digest_changed)``, so a file costs a few columns, not
    its rows' blocks in Python."""
    bad: list[str] = []
    for df in view.pair_files("X19"):
        if view.readable(df) and not view.has_base(df):
            base_gap(view, df, bad)
        stage = df.header.get("stage")
        j = view.base_join(df, X19_FIELDS)
        if j is None or j.cur.num_rows == 0:
            continue
        changed = pc.or_(
            _differs(_digest(j.cur, "identity", "members_digest", df.file_id),
                     _digest(j.base, "identity", "members_digest", df.file_id)),
            _differs(_digest(j.cur, "metadata", "fields_digest", df.file_id),
                     _digest(j.base, "metadata", "fields_digest", df.file_id)))
        memo: dict[tuple[Any, Any, bool], bool] = {}
        moves = zip(j.cur.column("doc_id").to_pylist(), j.base.column("state").to_pylist(),
                    j.cur.column("state").to_pylist(), changed.to_pylist(), strict=True)
        for d, a, b, ch in moves:
            key = (a, b, bool(ch))
            ok = memo.get(key)
            if ok is None:
                ok = memo[key] = _move_ok(a, b, bool(ch), stage)
            if not ok:
                bad.append(f"{df.file_id}: {d} {a} -> {b} ({stage})")
    return _finding("state_monotone", "{n} state move(s) not allowed", bad)


# ---------------------------------------------------------------- X20
@register("X20", "triage_current", scope="store")
def triage_current(view: StoreView) -> list[Finding]:
    """X20 (module docstring)."""
    bad: list[str] = []
    for chain_id, files in view.chains().items():
        if files[0].spec is None or files[0].spec.kind != "documents":
            continue
        res = view.resolve(chain_id)
        if isinstance(res, store_chain.ChainError):
            continue  # V11
        tbl = view.chain_view(res, ["doc_id", "state", "identity", "metadata", "annotations"])
        if tbl is None:
            continue  # a file whose data does not read (S1's finding)
        for start in range(0, tbl.num_rows, BATCH_ROWS):
            for r in tbl.slice(start, BATCH_ROWS).to_pylist():
                if r["state"] in ("identified", "retired") or r["identity"]["is_masked"]:
                    continue
                t = gate.triage_of(r)
                if t is None:
                    continue  # S8's
                if (t["basis_kind"], t["basis_digest"]) != (gate.BASIS_KIND, r["metadata"]["fields_digest"]):
                    bad.append(f"{chain_id}: {r['doc_id']} {r['state']} on basis "
                               f"{t['basis_kind']}:{(t['basis_digest'] or '')[:12]}, fields "
                               f"{r['metadata']['fields_digest'][:12]}")
                elif t["is_basis_current"] is False:
                    bad.append(f"{chain_id}: {r['doc_id']} {r['state']} with is_basis_current false")
    return _finding("triage_current", "{n} row(s) not triaged on their current fields", bad)
