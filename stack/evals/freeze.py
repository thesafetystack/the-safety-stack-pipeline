"""The eval-set freezer (track E, E3/E7): sample documents from a store's ``documents`` head, freeze
each item's model input exactly as the production workflow builds it, split dev/test, and write
``evals/<task>/<set-id>/{items.jsonl,manifest.json}`` plus labeler packets.

``python -m stack.evals.freeze triage --store v4/store-dev --in data/documents/<series>/ --set 2026-10-12a
--out evals/triage/2026-10-12a --packets <dir> [--seed S]``; ``... screen ... [--dry-run] [--n 80]``;
``... affiliation ... [--dry-run] [--n 30] [--doc-ids a,b,c --split test]``.

**Read-only.** The freezer resolves the ``documents`` version ``--in`` names, a series directory (its
last file) or an exact file (:func:`stack.store.chain.resolve`, no lock; required, there is no default
series: D382), reads its view at the head it resolved (the manifest records ``documents_in`` as given
and ``documents_head``) and reads text blobs; it writes nothing under the store.

**Population.** Live rows only: ``identity.is_masked`` false, ``identity.merged_into`` NULL, non-empty
``metadata.title``. Each row is placed by its anchor channel (:func:`stack.gate.anchor_channel`) and that
channel's ``channel_group`` (``channels.yaml`` through :func:`stack.gate.gate_view`).

**Production inputs, not re-implementations.**

* triage: the gate-view row (:func:`stack.gate.gate_view`), the in-band excerpt read by the asker's own
  reader (``workflows.triage.ask._excerpt``), ``record = workflows.triage.ask.document_text(row, excerpt)``
  (the per-call user message), ``production_asks = has_basis(row)``. Paper and forum rows without a basis
  are skipped (counted); url rows enter as title-only probes (``production_asks: false``).
* screen: the text read by the annotate framework's reader (``stack.annotate.stage._text``) and the user
  message captured from ``workflows.annotate.screen.main.ask`` itself, through a client that records the
  first ``complete`` call and stops (:class:`_Capture`); no model is called. ``text`` is the screen's
  ``_truncate(text, _MAX_DOC_CHARS)``, asserted to be the tail of the captured message. The whole text
  (what ``_text`` returns, which tier 2 receives and quotes anchor against) is frozen beside the set as
  ``texts/<item_id>.txt.gz`` (gzip, mtime 0: the same text gives the same bytes), with
  ``input.full_text_digest`` (sha256 of the text) and ``input.full_text_chars``. The whole texts are
  third-party documents and are not committed (``.gitignore``; D790): a reader looks in the set's own
  ``texts/`` first, then in the eval-text cache (:func:`cached_text_path`, ``$STACK_EVAL_TEXTS_DIR``), which
  ``tools/fetch_eval_texts.py`` fills from the archive.
* affiliation (:func:`freeze_affiliation`): paper rows with an acquired paper body (``content.current_role``
  in :data:`PAPER_BODY_ROLES`: ``full_text``, or a structured text standing in for it, ``xml``/``html``, as
  std-v3's ``annotate.affiliation`` filter; D485: under ``arxiv_pdf: on_miss | never`` most arXiv papers
  have only the HTML). The pool count of the others keeps its name ``paper_without_full_text``. ``window = header_window(text)``, ``authors_meta`` = the metadata author names passed
  through the v3 runner's ``_roster``, ``authors_block = build_authors_block(roster)``, ``user =
  _user_message(authors_block, window)`` (``workflows/annotate/affiliation/main.py`` and its
  ``extract.py``, imported, not copied); ``xml_snippet`` = the contrib/aff (JATS) or author-group
  (Elsevier) elements of the row's newest ``xml``-role raw blob, ``xml_byline`` = ``parse_xml_byline``
  of it (null without one, or when it finds no byline); ``roster_entries`` = the ``registry.yaml`` orgs
  that ``Canon.orgs_in`` finds in the window or the XML snippet, or whose domain an email in the window
  carries. ``production_uses_llm`` is false when ``xml_byline`` is set (production's XML tier answers
  without a model). Order: rows with XML first, then rows with a clear byline (:func:`clear_byline`),
  then the rest, each in hash order; ``stratum.proxy`` is ``xml`` / ``byline`` / ``no_byline``.
  ``--doc-ids`` replaces the sampling: exactly those documents of the pool (any proxy), no ``--n``; an
  id not in the pool (not in the head, not live, not a paper, no full text) is refused, as is a repeat.
  ``--split dev|test`` puts every item on that side instead of the balanced split (a stress set).

**Sampling.** Within a pool, candidates are ordered by ``sha256(seed + doc_id)`` and taken first-k.
Relevance proxy (``stratum.proxy``): :data:`SAFETY_STEMS` over title + abstract + excerpt (screen: title +
the sent text), case-insensitive; ``kw_high`` = >= 2 distinct stems, or >= 1 in the title. Each quota slot
aims at half ``kw_high`` (takes what exists). Triage hard cases (:func:`hard_tag`; ``governance``,
``policy``, ``capabilities``, ``safety_marketing``) are picked first per channel group
(:data:`TRIAGE_HARD`), round-robin over tags in hash order, and count toward the channel quotas; the
remaining quota is filled from rows without a hard tag (only when those run out from hard-tagged rows,
which keep their tag), so ``hard`` is set exactly on the items the hard pass chose plus any such fallback.
A channel that runs short passes its deficit to the other channels of its group.

**Split.** Strata ``(channel_group, proxy, hard is not None)`` in sorted order; inside each, hash order
alternating dev/test, starting with whichever side is behind overall, so each stratum and the whole set
are balanced to within one item. Item ids (``tri-0001`` …) follow hash order, so packets mix channels.

**Packets** (labeler input; nothing about strata, proxy or hard tags): triage ``batch-NN.jsonl`` of 40
``{"item_id", "record"}``; screen of 20 ``{"item_id", "title", "text"}`` (``text`` = the tier-1 text);
affiliation of 10 ``{"item_id", "window", "authors_meta", "xml_byline", "roster_entries"}``. Existing
``batch-*.jsonl`` in
the packet directory are removed first. An existing ``items.jsonl`` in ``--out`` is refused without
``--force`` (a frozen set is not silently replaced).
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import hashlib
import inspect
import json
import math
import os
import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Annotated, Any

import pyarrow as pa
import typer

from stack import gate
from stack.annotate import stage as annotate_stage
from stack.annotate.basis import basis_of
from stack.annotate.stage import AskContext, AskItem, PromptPin
from stack.rules import SAFETY_STEMS
from stack.store.blobs import BlobStore, open_blobs
from stack.store.chain import read_view, resolve

Row = dict[str, Any]

#: ``content.current_role`` values of a paper body the affiliation freeze takes (module docstring; D485).
PAPER_BODY_ROLES: frozenset[str] = frozenset({"full_text", "xml", "html"})
REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------- keyword heuristics
# Safety stems for the relevance proxy: ``SAFETY_STEMS``, imported from :mod:`stack.rules` (round 5
# (RUN) B7a moved the list there, where the collectors' filters read it too).
#: Policy terms (hard tag ``policy``): public policy, law and regulation. RL's (on-/off-policy,
#: policy gradient, "neural network policies") and science's laws (scaling, power, Goodhart's) excluded.
POLICY_TERMS: tuple[tuple[str, str], ...] = (
    ("policy", (r"(?<!on-)(?<!off-)(?<!on )(?<!off )(?<!network )(?<!rl )(?<!agent )\bpolic(y|ies)\b"
                r"(?![- ](gradient|optimi[sz]|network|learning|model|iteration|distill|improvement|search"
                r"|evaluation|update|space|entropy|head|mirror|collapse|loss|representation))")),
    ("regulat", r"\bregulat(ion|ions|ory|ors?|ing ai)\b"),
    ("legislat", r"\blegislat"),
    ("law", r"(?<!scaling )(?<!power )(?<!goodhart's )(?<!goodhart’s )\blaws?\b(?! of\b)"),
    ("act", r"\b(ai|eu|safety|chips|frontier ai) act\b|\bexecutive order"),
    ("export control", r"\bexport control"),
    ("liability", r"\bliabilit"),
)
#: Governance terms (hard tag ``governance``).
GOVERNANCE_TERMS: tuple[tuple[str, str], ...] = (
    ("governance", r"\bgovernance"),
    ("safety framework", r"\b(safety|preparedness|frontier|scaling) (policy|framework)"),
)
#: Weak policy/governance terms: count only in the title and only with a strong term in the text
#: (``audit`` and ``commitment`` are in plain research titles; ``framework`` is in too many to count).
GOVERNANCE_WEAK: tuple[tuple[str, str], ...] = (
    ("standard", r"\bstandards?\b"),
    ("audit", r"\baudit"),
    ("compliance", r"\bcomplian(ce|t)\b"),
    ("commitment", r"\bcommitment"),
    ("government", r"\bgovernment|\bcongress|\bsenate|\bparliament|\btreat(y|ies)\b|\blawmaker"),
)
#: RL context: there ``policy`` is the agent's, not a public one (the ``policy`` term is ignored).
RL_CONTEXT = r"\breinforcement learning|\brl\b|\breward|\b(on|off)[- ]policy|\bpolicy gradient"
#: How much of a long body the hard-case heuristics read (an excerpt's lead, not the whole post).
HARD_BODY_CHARS = 800
#: Capability terms (hard tag ``capabilities``, only when no safety stem matches anywhere).
CAPABILITY_TERMS: tuple[tuple[str, str], ...] = (
    ("benchmark", r"\bbenchmark"),
    ("sota", r"\bsota\b|\bstate[- ]of[- ]the[- ]art"),
    ("reasoning", r"\breasoning\b"),
    ("agent", r"\bagent(s|ic)?\b"),
    ("coding", r"\bcoding\b|\bcode generation|\bprogramming"),
    ("math", r"\bmath(s|ematical)?\b"),
    ("scaling", r"\bscaling\b"),
    ("multimodal", r"\bmulti-?modal"),
    ("introducing", r"\bintroduc(ing|es)\b"),
    ("model launch", r"\blaunch|\bnow available|\brelease of\b|\bgpt-?\d|\bgemini \d|\bclaude \w+ \d"),
)
#: Safety-marketing words (hard tag ``safety_marketing``, url documents' titles only) ...
MARKETING_WORDS = (r"\bsaf(e|er|ety|ely|eguard)\b|\btrust|\bresponsib|\bsecur|\bprivacy\b|\bprivate\b"
                   r"|\bprotect")
#: ... next to a product or feature.
PRODUCT_WORDS = (r"\bchatgpt|\bgpt\b|\bgpt-?\d|\bsora\b|\bcodex\b|\bgemini\b|\bclaude\b|\bbard\b|\bandroid"
                 r"|\bchrome\b|\bsearch\b|\bworkspace\b|\bpixel\b|\byoutube\b|\bgmail\b|\bapi\b|\bapps?\b"
                 r"|\bfeatures?\b|\btools?\b|\benterprise\b|\bbusiness\b|\bteams?\b|\baccounts?\b"
                 r"|\bcontrols\b|\bsettings\b|\bmode\b|\bbrowser\b|\bplatform\b|\bproducts?\b|\batlas\b"
                 r"|\boperator\b")
#: "Next to": the gap between a marketing word and a product word, in characters.
NEXT_TO_CHARS = 30
HARD_TAGS = ("governance", "policy", "capabilities", "safety_marketing")


def _compile(terms: Iterable[tuple[str, str]]) -> list[tuple[str, re.Pattern[str]]]:
    return [(n, re.compile(p, re.IGNORECASE)) for n, p in terms]


_SAFETY = _compile(SAFETY_STEMS)
_POLICY = _compile(POLICY_TERMS)
_GOV = _compile(GOVERNANCE_TERMS)
_GOV_WEAK = _compile(GOVERNANCE_WEAK)
_CAP = _compile(CAPABILITY_TERMS)
_RL = re.compile(RL_CONTEXT, re.IGNORECASE)
_MARKETING = re.compile(MARKETING_WORDS, re.IGNORECASE)
_PRODUCT = re.compile(PRODUCT_WORDS, re.IGNORECASE)


def _hits(terms: Sequence[tuple[str, re.Pattern[str]]], text: str) -> set[str]:
    return {n for n, p in terms if p.search(text)}


def proxy(title: str, body: str) -> str:
    """``kw_high`` when the title has a safety stem or title + body have >= 2 distinct ones."""
    if _hits(_SAFETY, title) or len(_hits(_SAFETY, title + "\n" + body)) >= 2:
        return "kw_high"
    return "kw_low"


def _next_to(a: re.Pattern[str], b: re.Pattern[str], text: str) -> bool:
    """Some match of ``a`` and some match of ``b`` are at most :data:`NEXT_TO_CHARS` apart."""
    spans_b = [m.span() for m in b.finditer(text)]
    return any(max(sb[0] - ea, sa - sb[1]) <= NEXT_TO_CHARS
               for sa, ea in (m.span() for m in a.finditer(text)) for sb in spans_b)


def hard_tag(group: str, title: str, abstract: str | None, excerpt: str | None) -> str | None:
    """The a-priori hard-case tag (module docstring), first match:

    * ``safety_marketing``: a url document whose title has a marketing word next to (within
      :data:`NEXT_TO_CHARS`) a product word;
    * ``policy`` / ``governance``: a strong (policy or governance) term in the title, or >= 2
      distinct strong terms, or one strong term and a weak term in the title, where the text is the
      title, abstract and the first :data:`HARD_BODY_CHARS` of the excerpt;
      ``policy`` when a policy term is among them, else ``governance`` (in an RL context,
      :data:`RL_CONTEXT`, the term ``policy`` does not count);
    * ``capabilities``: a capability term in the title or >= 2 in that text, and no safety stem in
      the title, the abstract or the whole excerpt."""
    text = "\n".join(x for x in (title, abstract, (excerpt or "")[:HARD_BODY_CHARS]) if x)
    if group == "url" and _next_to(_MARKETING, _PRODUCT, title):
        return "safety_marketing"
    pol_t, gov_t = _hits(_POLICY, title), _hits(_GOV, title)
    pol, gov = _hits(_POLICY, text), _hits(_GOV, text)
    if _RL.search(text):
        pol_t, pol = pol_t - {"policy"}, pol - {"policy"}
    if pol_t or gov_t or len(pol | gov) >= 2 or ((pol or gov) and _hits(_GOV_WEAK, title)):
        return "policy" if pol_t or (not gov_t and pol) else "governance"
    if (not _hits(_SAFETY, "\n".join(x for x in (title, abstract, excerpt) if x))
            and (_hits(_CAP, title) or len(_hits(_CAP, text)) >= 2)):
        return "capabilities"
    return None


# ---------------------------------------------------------------- quotas
#: Triage quotas: channel group -> slot -> k; slot ``"*"`` pools every channel of the group.
TRIAGE_QUOTAS: dict[str, dict[str, int]] = {
    "paper": {"openalex": 45, "arxiv": 35, "semantic_scholar": 20},
    "forum": {"*": 40},
    "url": {"openai_blog": 24, "google_research": 10, "anthropic_alignment_blog": 8,
            "transformer_circuits": 6, "openai_alignment_blog": 6, "anthropic_cards": 6},
}
#: Hard cases picked first per channel group (they count toward the channel quotas).
TRIAGE_HARD: dict[str, int] = {"paper": 15, "forum": 5, "url": 20}
TRIAGE_PACKET = 40
SCREEN_N = 80
SCREEN_PACKET = 20
AFFILIATION_N = 30
AFFILIATION_PACKET = 10
#: The XML snippet's cap in characters (a labeler input, not a model input).
XML_SNIPPET_CHARS = 20_000


# ---------------------------------------------------------------- candidates
@dataclass
class Cand:
    """One live document as a sampling candidate: its placement, proxy, hard tag and item body."""

    doc_id: str
    order: str  # sha256(seed + doc_id)
    group: str
    channel: str
    proxy: str
    hard: str | None
    eligible: bool
    item: Row = field(default_factory=dict)  # license_class, production_asks, input, input_digest
    full_text: str | None = None  # screen: the whole text, frozen beside the set (not in items.jsonl)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def order_key(seed: str, doc_id: str) -> str:
    return sha256_hex(seed + doc_id)


def is_live(row: Mapping[str, Any]) -> bool:
    ident, md = row["identity"], row["metadata"]
    return not ident.get("is_masked") and ident.get("merged_into") is None and bool(md.get("title"))


@dataclass
class Source:
    """The documents view at the resolved head and the store's blob reader."""

    store: Path
    head: str
    rows: list[Row]
    blobs: BlobStore
    population: dict[str, int]
    documents: str  # the ``--in`` it was loaded from, as given (the manifests' ``documents_in``)


def load_source(store: Path, documents: str) -> Source:
    """The live rows of the ``documents`` version ``documents`` names (``data/documents/<series>/``,
    its last file, or an exact file; no default series, D382) as gate-view rows (read-only)."""
    chain = resolve(store, documents)
    table = read_view(chain)
    rows = table.to_pylist()
    live = [r for r in rows if is_live(r)]
    view = gate.gate_view(pa.Table.from_pylist(live, schema=table.schema), gate.channel_groups()).to_pylist()
    pop = {"rows": len(rows), "live": len(live), "masked": sum(bool(r["identity"].get("is_masked")) for r in rows),
           "merged": sum(r["identity"].get("merged_into") is not None for r in rows),
           "untitled": sum(not r["metadata"].get("title") for r in rows)}
    return Source(store, chain.head, view, open_blobs(store), pop, documents)


# ---------------------------------------------------------------- triage items
def triage_cands(src: Source, seed: str) -> list[Cand]:
    """Every live row as a triage candidate with its frozen item body (module docstring)."""
    from workflows.triage import ask as triage_ask

    reader = SimpleNamespace(blobs=src.blobs)
    out = []
    for r in src.rows:
        md = r["metadata"]
        excerpt = triage_ask._excerpt(reader, r.get("inband_text_sha256"))  # type: ignore[arg-type]
        record = triage_ask.document_text(r, excerpt)
        asks = triage_ask.has_basis(r)
        group, channel = r["channel_group"], gate.anchor_channel(r)
        title = md.get("title") or ""
        body = "\n".join(x for x in (md.get("abstract"), excerpt) if x)
        out.append(Cand(
            doc_id=r["doc_id"], order=order_key(seed, r["doc_id"]), group=group, channel=channel,
            proxy=proxy(title, body), hard=hard_tag(group, title, md.get("abstract"), excerpt),
            eligible=group in TRIAGE_QUOTAS and (asks or group == "url"),
            item={"license_class": basis_of(r, "fields").license_class, "production_asks": asks,
                  "input": {"title": md.get("title"),
                            "authors": [a["name"] for a in md.get("authors") or [] if a.get("name")],
                            "abstract": md.get("abstract"), "excerpt": excerpt, "record": record},
                  "input_digest": sha256_hex(record)}))
    return out


# ---------------------------------------------------------------- screen items
class _Captured(Exception):
    """Raised by :class:`_Capture` after it recorded the first call: the screen asks nothing more."""


class _Capture:
    """An LLM client stand-in that records the first ``complete`` call's ``user`` and stops."""

    def __init__(self) -> None:
        self.user: str | None = None

    def complete(self, **kw: Any) -> Any:
        self.user = kw["user"]
        raise _Captured


def screen_message(row: Row, text: str) -> str:
    """The tier-1 user message ``workflows.annotate.screen.main.ask`` sends for ``row`` with
    ``text`` (captured from the production function; no model is called)."""
    import workflows.annotate.screen.main as screen_wf

    b = basis_of(row, "text")
    assert b.digest is not None
    item = AskItem(doc_id=row["doc_id"], source_id=b.source_id, basis_kind="text", basis_digest=b.digest,
                   fields=None, row=row, license_class=b.license_class or "restricted", attempt=1,
                   load_text=lambda: text)
    cap = _Capture()
    actx = AskContext(run_id="eval-freeze", stage="annotate.screen", store_root=None, pin=PromptPin(),
                      plan_pin={}, options={}, budget_usd=None, _config=annotate_stage.stack_config,
                      _client=cap)  # type: ignore[arg-type]
    try:
        screen_wf.ask(item, actx)
    except _Captured:
        pass
    if cap.user is None:
        raise RuntimeError(f"screen freeze: {row['doc_id']}: the screen made no call to capture")
    return cap.user


def screen_cands(src: Source, seed: str) -> list[Cand]:
    """Every live row with a current text as a screen candidate (module docstring)."""
    import workflows.annotate.screen.main as screen_wf

    reader = SimpleNamespace(blobs=src.blobs, stage="annotate.screen")
    out = []
    for r in src.rows:
        b = basis_of(r, "text")
        if b.digest is None:
            continue
        full = annotate_stage._text(reader, b.digest)  # type: ignore[arg-type]
        user = screen_message(r, full)
        sent = screen_wf._truncate(full, screen_wf._MAX_DOC_CHARS)
        if not user.endswith(sent):
            raise RuntimeError(f"screen freeze: {r['doc_id']}: the captured message does not end with "
                               f"the truncated text (screen's message layout changed)")
        md = r["metadata"]
        title = md.get("title") or ""
        group, channel = r["channel_group"], gate.anchor_channel(r)
        out.append(Cand(
            doc_id=r["doc_id"], order=order_key(seed, r["doc_id"]), group=group, channel=channel,
            proxy=proxy(title, sent), hard=None, eligible=True, full_text=full,
            item={"license_class": b.license_class, "production_asks": True,
                  "input": {"title": md.get("title"), "text": sent, "text_digest": b.digest,
                            "basis_role": (r.get("content") or {}).get("current_role"),
                            "full_text_digest": sha256_hex(full), "full_text_chars": len(full)},
                  "input_digest": sha256_hex(user)}))
    return out


TEXTS_DIR = "texts"


#: The eval-text cache (D790): the frozen sets' whole texts are third-party documents, kept out of the
#: repository. ``$STACK_EVAL_TEXTS_DIR``, else ``$XDG_CACHE_HOME/stack/eval-texts``, else
#: ``~/.cache/stack/eval-texts``; laid out ``<root>/<task>/<set_id>/<item_id>.txt.gz`` (the same bytes as
#: the set's ``texts/`` would hold). ``tools/fetch_eval_texts.py`` fills it.
TEXTS_CACHE_ENV = "STACK_EVAL_TEXTS_DIR"
#: The command a missing text's message names.
FETCH_TEXTS_HINT = "uv run --no-sync python tools/fetch_eval_texts.py"
#: Where the fetch tool reads from, for messages: an outsider cannot fill the cache from the store's bucket.
ARCHIVE_NOTE = ("the archive it reads is in the maintainers' private storage; `--from-tar` takes a copy of it; "
                "docs/SETUP.md §7")


def text_path(set_dir: Path, item_id: str) -> Path:
    """Where a screen item's whole text is frozen: ``<set>/texts/<item_id>.txt.gz``."""
    return set_dir / TEXTS_DIR / f"{item_id}.txt.gz"


def texts_cache_root() -> Path:
    """The eval-text cache's root (:data:`TEXTS_CACHE_ENV`, else the XDG cache directory)."""
    env = os.environ.get(TEXTS_CACHE_ENV, "").strip()
    if env:
        return Path(env).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
    return (Path(xdg).expanduser() if xdg else Path.home() / ".cache") / "stack" / "eval-texts"


def set_key(set_dir: Path) -> tuple[str, str]:
    """``(task, set_id)`` of a set directory: its ``manifest.json``'s ``task`` and ``set_id`` (a copy of a
    set under another name keeps its key), else the directory's parent's name and its own."""
    m = set_dir / "manifest.json"
    if m.is_file():
        try:
            man = json.loads(m.read_text("utf-8"))
        except (OSError, ValueError):
            man = {}
        if isinstance(man, dict) and isinstance(man.get("task"), str) and isinstance(man.get("set_id"), str):
            return man["task"], man["set_id"]
    return set_dir.resolve().parent.name, set_dir.resolve().name


def cached_text_path(set_dir: Path, item_id: str) -> Path:
    """An item's whole text in the eval-text cache: ``<cache>/<task>/<set_id>/<item_id>.txt.gz``."""
    task, set_id = set_key(set_dir)
    return texts_cache_root() / task / set_id / f"{item_id}.txt.gz"


def find_text(set_dir: Path, item_id: str) -> Path | None:
    """The item's frozen whole text: the set's own ``texts/`` file (a fresh freeze, a test's copy), else
    the cache's; None when neither exists."""
    for p in (text_path(set_dir, item_id), cached_text_path(set_dir, item_id)):
        if p.is_file():
            return p
    return None


#: Eval files with long third-party excerpts kept out of the repository (D793): ``<evals>/ARCHIVED_PINS`` lists
#: ``<sha256>  <path under evals/>`` for each; the cache holds them at ``<cache root>/<path under evals/>``.
ARCHIVED_PINS = "archived.sha256"


def archived_pins(evals_root: Path) -> dict[str, str]:
    """``{path under evals/: sha256}`` of the eval files kept out of the repository (:data:`ARCHIVED_PINS`)."""
    p = evals_root / ARCHIVED_PINS
    if not p.is_file():
        return {}
    return {ln.split()[1]: ln.split()[0] for ln in p.read_text("utf-8").splitlines() if len(ln.split()) == 2}


def find_eval_file(evals_root: Path, rel: str) -> Path | None:
    """An eval file by its path under ``evals/``: the repository's copy, else the eval-text cache's
    (``<cache root>/<rel>``, D793); None when neither exists."""
    for p in (evals_root / rel, texts_cache_root() / rel):
        if p.is_file():
            return p
    return None


def items_file(set_dir: Path) -> Path:
    """A set's ``items.jsonl``: its own, else the eval-text cache's (``<cache>/<task>/<set_id>/items.jsonl``; the
    items of the sets whose inputs carry long third-party excerpts are archived outside the repository, D795);
    the set's own path when neither exists, so a reader's error names it."""
    own = set_dir / "items.jsonl"
    if own.is_file():
        return own
    task, set_id = set_key(set_dir)
    cached = texts_cache_root() / task / set_id / "items.jsonl"
    return cached if cached.is_file() else own


def missing_text_message(set_dir: Path, item_id: str) -> str:
    """Why an item's whole text cannot be read, and how to fetch it."""
    return (f"{item_id}: whole text not found ({text_path(set_dir, item_id)} nor the eval-text cache's "
            f"{cached_text_path(set_dir, item_id)}; ${TEXTS_CACHE_ENV} sets the cache, "
            f"`{FETCH_TEXTS_HINT}` fills it; {ARCHIVE_NOTE})")


def gzip_text(text: str) -> bytes:
    """Deterministic gzip of ``text`` (UTF-8, mtime 0, no file name): the same text, the same bytes."""
    return gzip.compress(text.encode("utf-8"), compresslevel=9, mtime=0)


def read_text_gz(path: Path) -> str:
    return gzip.decompress(path.read_bytes()).decode("utf-8")


def write_texts(out: Path, items: Sequence[Row], full: Mapping[str, str]) -> None:
    """``texts/<item_id>.txt.gz`` for every item (``full``: doc_id -> text); stale files removed."""
    d = out / TEXTS_DIR
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.txt.gz"):
        old.unlink()
    for it in items:
        text = full[it["doc_id"]]
        if sha256_hex(text) != it["input"]["full_text_digest"]:
            raise RuntimeError(f"screen freeze: {it['item_id']}: full text does not match its digest")
        text_path(out, it["item_id"]).write_bytes(gzip_text(text))


# ---------------------------------------------------------------- selection
def _fill(cands: Sequence[Cand], n: int, have_high: int, slot_total: int) -> list[Cand]:
    """Up to ``n`` of ``cands`` (hash order) so the slot of ``slot_total`` items, with
    ``have_high`` kw_high already, is about half kw_high; a short side is topped up by the other."""
    high = [c for c in cands if c.proxy == "kw_high"]
    low = [c for c in cands if c.proxy != "kw_high"]
    th = min(max(0, math.ceil(slot_total / 2) - have_high), len(high), n)
    tl = min(n - th, len(low))
    th = min(n - tl, len(high))
    return high[:th] + low[:tl]


def select(cands: Sequence[Cand], quotas: Mapping[str, Mapping[str, int]],
           hard_targets: Mapping[str, int]) -> tuple[list[Cand], dict[str, Any]]:
    """The sample (module docstring): hard picks first, then per-slot fills, then each group's
    deficit from the group's other slots. Returns the chosen candidates and a selection report."""
    pool = sorted((c for c in cands if c.eligible), key=lambda c: c.order)
    chosen: dict[str, Cand] = {}

    def slot_of(c: Cand) -> str | None:
        q = quotas.get(c.group, {})
        return c.channel if c.channel in q else ("*" if "*" in q else None)

    def in_slot(group: str, slot: str) -> list[Cand]:
        return [c for c in pool if c.group == group and slot_of(c) == slot]

    def taken(group: str, slot: str) -> list[Cand]:
        return [c for c in chosen.values() if c.group == group and slot_of(c) == slot]

    report: dict[str, Any] = {"short": {}, "hard_picked": {}}
    for group, slots in quotas.items():
        # 1. hard cases, round-robin over tags in hash order, within slot quotas
        want = hard_targets.get(group, 0)
        by_tag = {t: [c for c in pool if c.group == group and c.hard == t and slot_of(c) is not None]
                  for t in HARD_TAGS}
        got = 0
        while got < want and any(by_tag.values()):
            for t in HARD_TAGS:
                while by_tag[t]:
                    c = by_tag[t].pop(0)
                    s = slot_of(c)
                    assert s is not None
                    if len(taken(group, s)) < slots[s]:
                        chosen[c.doc_id] = c
                        got += 1
                        break
                if got >= want:
                    break
        report["hard_picked"][group] = got
        # 2. fill each slot: rows without a hard tag first, then hard-tagged leftovers
        deficit = 0
        for slot, k in slots.items():
            for hard_ok in (False, True):
                have = taken(group, slot)
                need = k - len(have)
                if need <= 0:
                    break
                rest = [c for c in in_slot(group, slot)
                        if c.doc_id not in chosen and (c.hard is not None) == hard_ok]
                for c in _fill(rest, need, sum(h.proxy == "kw_high" for h in have), k):
                    chosen[c.doc_id] = c
            short = k - len(taken(group, slot))
            if short:
                report["short"][f"{group}/{slot}"] = short
                deficit += short
        # 3. a short slot's deficit from the group's other slots (quota order)
        for slot, k in slots.items():
            if deficit <= 0:
                break
            for hard_ok in (False, True):
                rest = [c for c in in_slot(group, slot)
                        if c.doc_id not in chosen and (c.hard is not None) == hard_ok]
                have = taken(group, slot)
                for c in _fill(rest, deficit, sum(h.proxy == "kw_high" for h in have), len(have) + deficit):
                    chosen[c.doc_id] = c
                    deficit -= 1
                if deficit <= 0:
                    break
        if deficit:
            report["short"][f"{group}/*unfilled"] = deficit
    return sorted(chosen.values(), key=lambda c: c.order), report


def assign_split(chosen: Sequence[Cand]) -> dict[str, str]:
    """doc_id -> ``dev``/``test`` (module docstring)."""
    strata: dict[tuple[str, str, bool], list[Cand]] = collections.defaultdict(list)
    for c in chosen:
        strata[(c.group, c.proxy, c.hard is not None)].append(c)
    out: dict[str, str] = {}
    n = {"dev": 0, "test": 0}
    for key in sorted(strata):
        side = "dev" if n["dev"] <= n["test"] else "test"
        for c in sorted(strata[key], key=lambda c: c.order):
            out[c.doc_id] = side
            n[side] += 1
            side = "test" if side == "dev" else "dev"
    return out


def build_items(chosen: Sequence[Cand], prefix: str, split_all: str | None = None) -> list[Row]:
    """items.jsonl rows (design.md order), ids by hash order; ``split_all``: every item in that split
    (a holdout) instead of the dev/test halves."""
    split = {c.doc_id: split_all for c in chosen} if split_all else assign_split(chosen)
    items = []
    for i, c in enumerate(sorted(chosen, key=lambda c: c.order), 1):
        items.append({"item_id": f"{prefix}-{i:04d}", "doc_id": c.doc_id, "split": split[c.doc_id],
                      "stratum": {"channel_group": c.group, "channel": c.channel, "proxy": c.proxy},
                      "hard": c.hard, "license_class": c.item["license_class"],
                      "production_asks": c.item["production_asks"], "input": c.item["input"],
                      "input_digest": c.item["input_digest"]})
    return items


# ---------------------------------------------------------------- outputs
def jsonl(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows).encode("utf-8")


def write_packets(items: Sequence[Row], packets: Path, size: int,
                  pick: Callable[[Row], Row]) -> list[Path]:
    packets.mkdir(parents=True, exist_ok=True)
    for old in packets.glob("batch-*.jsonl"):
        old.unlink()
    out = []
    for b in range(0, len(items), size):
        p = packets / f"batch-{b // size + 1:02d}.jsonl"
        p.write_bytes(jsonl(pick(it) for it in items[b:b + size]))
        out.append(p)
    return out


def counts(items: Sequence[Row]) -> dict[str, Any]:
    """Counts per stratum x split and per single field (manifest and the console summary)."""
    def c(f: Callable[[Row], Any]) -> dict[str, int]:
        return dict(sorted(collections.Counter(str(f(it)) for it in items).items()))
    return {
        "total": len(items),
        "split": c(lambda it: it["split"]),
        "channel_group": c(lambda it: it["stratum"]["channel_group"]),
        "channel": c(lambda it: f"{it['stratum']['channel_group']}/{it['stratum']['channel']}"),
        "proxy": c(lambda it: it["stratum"]["proxy"]),
        "hard": c(lambda it: it["hard"]),
        "license_class": c(lambda it: it["license_class"]),
        "production_asks": c(lambda it: it["production_asks"]),
        "stratum_split": c(lambda it: f"{it['stratum']['channel_group']}|{it['stratum']['proxy']}|"
                                      f"{'hard' if it['hard'] else 'plain'}|{it['split']}"),
        "channel_proxy_hard_split": c(lambda it: f"{it['stratum']['channel_group']}/{it['stratum']['channel']}|"
                                                 f"{it['stratum']['proxy']}|{it['hard']}|{it['split']}"),
    }


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def provenance() -> dict[str, Any]:
    """Git commit and dirtiness of the repo, the freezer's own source digest, the basis builder's."""
    from workflows.triage import ask as triage_ask

    return {"git_commit": _git("rev-parse", "HEAD"),
            "git_dirty": bool(_git("status", "--porcelain", "--untracked-files=no")),
            "freezer": {"module": "stack.evals.freeze",
                        "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()},
            "basis_builder": {"ref": "workflows.triage.ask:document_text",
                              "source_sha256": sha256_hex(inspect.getsource(triage_ask.document_text)),
                              "caps": {"MAX_ABSTRACT_CHARS": triage_ask.MAX_ABSTRACT_CHARS,
                                       "MAX_EXCERPT_CHARS": triage_ask.MAX_EXCERPT_CHARS,
                                       "MAX_AUTHORS": triage_ask.MAX_AUTHORS}}}


def screen_provenance() -> dict[str, Any]:
    import workflows.annotate.screen.main as screen_wf

    p = provenance()
    p["basis_builder"] = {"ref": "workflows.annotate.screen.main:ask (tier-1 user message, captured)",
                          "source_sha256": sha256_hex(inspect.getsource(screen_wf.ask)),
                          "caps": {"_MAX_DOC_CHARS": screen_wf._MAX_DOC_CHARS}}
    return p


def population_report(cands: Sequence[Cand], quotas: Mapping[str, Any]) -> dict[str, Any]:
    by: dict[str, dict[str, int]] = collections.defaultdict(lambda: collections.Counter())
    for c in cands:
        k = f"{c.group}/{c.channel}"
        by[k]["live"] += 1
        by[k]["eligible"] += c.eligible
        by[k][c.proxy] += c.eligible
        if c.hard is not None and c.eligible:
            by[k][f"hard:{c.hard}"] += 1
        if not c.eligible:
            by[k]["skipped_no_basis" if c.group in quotas else "skipped_group_not_sampled"] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(by.items())}


def freeze(task: str, src: Source, set_id: str, out: Path, packets: Path, seed: str, *,
           force: bool = False) -> tuple[list[Row], dict[str, Any]]:
    """Freeze ``task`` (``triage``; ``screen`` through :func:`freeze_screen`) from ``src``
    (:func:`load_source`) and write the set."""
    if task != "triage":
        raise ValueError(f"freeze: task {task!r} (use freeze_screen for screen)")
    cands = triage_cands(src, seed)
    chosen, rep = select(cands, TRIAGE_QUOTAS, TRIAGE_HARD)
    items = build_items(chosen, "tri")
    manifest = _manifest(task, set_id, src, seed, items, provenance(), {
        "quotas": TRIAGE_QUOTAS, "hard_targets": TRIAGE_HARD, "selection": rep,
        "population": {**src.population, "by_channel": population_report(cands, TRIAGE_QUOTAS)}})
    _write(out, items, manifest, force)
    write_packets(items, packets, TRIAGE_PACKET, lambda it: {"item_id": it["item_id"],
                                                             "record": it["input"]["record"]})
    return items, manifest


def screen_quotas(cands: Sequence[Cand], n: int) -> dict[str, dict[str, int]]:
    """Screen slots: ``n`` over channels proportional to their pools (largest remainder, capped at
    the pool, at least one per non-empty channel while ``n`` allows)."""
    pools = collections.Counter((c.group, c.channel) for c in cands if c.eligible)
    total = sum(pools.values())
    if total <= n:
        alloc = dict(pools)
    else:
        raw = {k: n * v / total for k, v in pools.items()}
        alloc = {k: max(1, min(pools[k], math.floor(x))) for k, x in raw.items()}
        for key in sorted(raw, key=lambda c: (-(raw[c] - math.floor(raw[c])), c)):
            if sum(alloc.values()) >= n:
                break
            if alloc[key] < pools[key]:
                alloc[key] += 1
    q: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for (g, ch), k in sorted(alloc.items()):
        q[g][ch] = k
    return dict(q)


def freeze_screen(src: Source, set_id: str, out: Path, packets: Path, seed: str, *, n: int = SCREEN_N,
                  dry_run: bool = False, force: bool = False,
                  exclude: frozenset[str] = frozenset(), prefix: str = "scr",
                  split_all: str | None = None) -> tuple[list[Row], dict[str, Any]]:
    """The screen set (module docstring); ``dry_run`` returns the pool report and writes nothing.
    ``exclude``: doc ids never chosen (another set's items, so a holdout shares none); ``prefix`` the
    item-id prefix; ``split_all`` every item's split (:func:`build_items`)."""
    cands = [c for c in screen_cands(src, seed) if c.doc_id not in exclude]
    quotas = screen_quotas(cands, n)
    pool = {"documents_head": src.head, "population": src.population,
            "pool": population_report(cands, quotas), "quotas": quotas}
    if dry_run:
        return [], pool
    chosen, rep = select(cands, quotas, {})
    items = build_items(chosen, prefix, split_all)
    manifest = _manifest("screen", set_id, src, seed, items, screen_provenance(), {
        "quotas": quotas, "target_n": n, "selection": rep, "excluded_doc_ids": len(exclude),
        "population": {**src.population, "by_channel": pool["pool"]},
        "texts": f"{TEXTS_DIR}/<item_id>.txt.gz (gzip mtime 0; sha256 of the text = input.full_text_digest)"})
    _write(out, items, manifest, force)
    write_texts(out, items, {c.doc_id: c.full_text or "" for c in chosen})
    write_packets(items, packets, SCREEN_PACKET, lambda it: {"item_id": it["item_id"],
                                                             "title": it["input"]["title"],
                                                             "text": it["input"]["text"]})
    return items, manifest


# ---------------------------------------------------------------- affiliation items
#: How much of the window's head :func:`clear_byline` reads, and the share of metadata family
#: names it needs there.
BYLINE_CHARS = 3_000
BYLINE_SHARE = 0.5
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def _fold(text: str) -> str:
    """Lowercased, diacritics dropped (the name key's folding, without its comma un-inversion)."""
    nfd = unicodedata.normalize("NFD", text)
    return "".join(c for c in nfd if unicodedata.category(c) != "Mn").lower()


def clear_byline(window: str, names: Sequence[str]) -> bool:
    """At least :data:`BYLINE_SHARE` of the metadata authors' family names (the last token of the
    name key) appear as words in the first :data:`BYLINE_CHARS` of the window (no names: False)."""
    from stack.evals.gold import affiliation_extract

    if not names:
        return False
    head = _fold(window[:BYLINE_CHARS])
    fam = [k.split()[-1] for k in (affiliation_extract().norm_author_name(n) for n in names) if k.split()]
    hits = sum(bool(re.search(rf"\b{re.escape(_fold(f))}\b", head)) for f in fam)
    return bool(fam) and hits / len(fam) >= BYLINE_SHARE


def xml_blob(row: Row) -> Row | None:
    """The newest ``xml``-role raw snapshot of the row (by ``captured_at``), or None."""
    snaps = [s for s in ((row.get("content") or {}).get("snapshots") or [])
             if s.get("content_role") == "xml" and s.get("kind") == "raw"]
    return max(snaps, key=lambda s: str(s.get("captured_at") or "")) if snaps else None


def xml_snippet(raw: bytes) -> str | None:
    """The byline part of publisher XML: JATS ``contrib-group`` and ``aff`` elements outside them,
    else Elsevier ``author-group`` elements, serialised; capped at :data:`XML_SNIPPET_CHARS`. None
    when the XML does not parse or has none of them."""
    import xml.etree.ElementTree as ET

    from stack.evals.gold import affiliation_extract

    ex = affiliation_extract()
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    groups = list(ex._descendants(root, "contrib-group"))
    inner = {id(a) for g in groups for a in ex._descendants(g, "aff")}
    parts = groups + [a for a in ex._descendants(root, "aff") if id(a) not in inner]
    if not parts:
        parts = list(ex._descendants(root, "author-group"))
    if not parts:
        return None
    text = "\n".join(ET.tostring(el, encoding="unicode").strip() for el in parts)
    return text[:XML_SNIPPET_CHARS] + ("\n…[cut]" if len(text) > XML_SNIPPET_CHARS else "")


def byline_json(authors: Sequence[Any] | None) -> list[Row] | None:
    """``parse_xml_byline`` output as JSON (None stays None)."""
    if not authors:
        return None
    return [{"author_surface": a.author_surface, "email": a.email or None, "orcid": a.orcid or None,
             "affiliations": [{"surface": f.surface, "marker": f.marker} for f in a.affiliations]}
            for a in authors]


def roster_entries(canon: Any, orgs: Mapping[str, Any], *texts: str | None) -> list[Row]:
    """The registry orgs (``registry.yaml``, file order) that ``canon.orgs_in`` finds in any of
    ``texts`` or whose domain an email in them carries: ``{id, name, aliases, domains}``."""
    from stack.canon import email_domain

    hit: set[str] = set()
    for t in texts:
        if not t:
            continue
        hit.update(r.slug for r in canon.orgs_in(t) if r.slug)
        for m in _EMAIL.finditer(t):
            d = canon.org_for_domain(email_domain(m.group(0)))
            if d.slug:
                hit.add(d.slug)
    return [{"id": slug, "name": o.name, "aliases": list(o.aliases), "domains": list(o.domains)}
            for slug, o in orgs.items() if slug in hit]


def affiliation_user(authors_meta: Sequence[str], window: str) -> tuple[list[str], str, str]:
    """``(roster, authors_block, user)`` as the v3 runner builds them: ``_roster`` over the metadata
    author names (main.py reads them as ``meta.extra["authors"]``), ``build_authors_block``,
    ``_user_message``."""
    from stack.evals.gold import affiliation_runner

    main = affiliation_runner()
    roster = main._roster(SimpleNamespace(extra={"authors": list(authors_meta)}))
    block = main.ex.build_authors_block(roster)
    return roster, block, main._user_message(block, window)


AFF_RANK = {"xml": 0, "byline": 1, "no_byline": 2}


def affiliation_cands(src: Source, seed: str) -> tuple[list[Cand], dict[str, int]]:
    """Paper rows with an acquired full text as affiliation candidates (module docstring), and the
    pool counts."""
    from stack.annotate.stage import stack_config
    from stack.canon import Canon
    from stack.evals.gold import affiliation_extract

    ex = affiliation_extract()
    cfg = stack_config()
    canon = Canon.from_config(cfg)
    orgs = {o.slug: o for o in cfg.registry.orgs}
    reader = SimpleNamespace(blobs=src.blobs, stage="annotate.affiliation")
    out: list[Cand] = []
    pool: collections.Counter[str] = collections.Counter()
    for r in src.rows:
        content = r.get("content") or {}
        if r["channel_group"] != "paper":
            continue
        b = basis_of(r, "text")
        if b.digest is None or content.get("current_role") not in PAPER_BODY_ROLES:
            pool["paper_without_full_text"] += 1
            continue
        text = annotate_stage._text(reader, b.digest)  # type: ignore[arg-type]
        window = ex.header_window(text)
        md = r["metadata"]
        names = [a["name"] for a in md.get("authors") or [] if a.get("name")]
        roster, block, user = affiliation_user(names, window)
        blob = xml_blob(r)
        snippet = byline = None
        if blob is not None:
            try:
                raw = src.blobs.get(blob["raw"]["sha256"])
            except KeyError:  # BlobNotFound: a pruned raw blob degrades the XML tier, as in production
                pool["xml_blob_missing"] += 1
                raw = None
            if raw is not None:
                snippet = xml_snippet(raw)
                byline = byline_json(ex.parse_xml_byline(raw))
        prox = "xml" if blob is not None else ("byline" if clear_byline(window, roster) else "no_byline")
        pool[prox] += 1
        pool["xml_byline_parsed"] += byline is not None
        out.append(Cand(
            doc_id=r["doc_id"], order=order_key(seed, r["doc_id"]), group="paper", channel=gate.anchor_channel(r),
            proxy=prox, hard=None, eligible=True,
            item={"license_class": b.license_class, "production_asks": byline is None,
                  "production_uses_llm": byline is None,
                  "input": {"title": md.get("title"), "window": window, "authors_meta": roster,
                            "authors_block": block, "user": user, "xml_snippet": snippet,
                            "xml_byline": byline,
                            "roster_entries": roster_entries(canon, orgs, window, snippet),
                            "text_digest": b.digest, "full_text_chars": len(text)},
                  "input_digest": sha256_hex(user)}))
    pool["eligible"] = len(out)
    return out, dict(sorted(pool.items()))


def freeze_affiliation(src: Source, set_id: str, out: Path, packets: Path, seed: str, *,
                       n: int = AFFILIATION_N, dry_run: bool = False, force: bool = False,
                       doc_ids: Sequence[str] | None = None,
                       split: str | None = None) -> tuple[list[Row], dict[str, Any]]:
    """The affiliation set (module docstring); ``dry_run`` returns the pool and writes nothing.
    ``doc_ids`` selects exactly those pool documents (ValueError on an unknown or repeated id);
    ``split`` sets every item's split."""
    from stack.evals.gold import affiliation_extract

    cands, pool = affiliation_cands(src, seed)
    report = {"documents_head": src.head, "population": src.population, "pool": pool,
              "by_channel": population_report(cands, {"paper": {}})}
    if dry_run:
        return [], report
    if split not in (None, "dev", "test"):
        raise ValueError(f"freeze_affiliation: split {split!r} (dev, test or None)")
    if doc_ids is not None:
        by_id = {c.doc_id: c for c in cands}
        dup = sorted(d for d, k in collections.Counter(doc_ids).items() if k > 1)
        unknown = [d for d in doc_ids if d not in by_id]
        if not doc_ids:
            raise ValueError("freeze_affiliation: empty doc_ids")
        if dup or unknown:
            raise ValueError(f"freeze_affiliation: doc ids not in the affiliation pool at {src.head}: "
                             f"{unknown}; repeated: {dup}")
        chosen = [by_id[d] for d in doc_ids]
    else:
        chosen = sorted(cands, key=lambda c: (AFF_RANK[c.proxy], c.order))[:n]
    items = build_items(chosen, "aff")
    if split is not None:
        for it in items:
            it["split"] = split
    for it, c in zip(items, sorted(chosen, key=lambda c: c.order), strict=True):
        it["production_uses_llm"] = c.item["production_uses_llm"]
    ex = affiliation_extract()
    extract_py = Path(str(ex.__file__))
    prov = provenance()
    prov["basis_builder"] = {
        "ref": ("workflows/annotate/affiliation/main.py:_roster,_user_message + "
                "extract.py:header_window,build_authors_block (user message); parse_xml_byline (xml_byline)"),
        "source_sha256": {"main.py": hashlib.sha256(extract_py.with_name("main.py").read_bytes()).hexdigest(),
                          "extract.py": hashlib.sha256(extract_py.read_bytes()).hexdigest()},
        "caps": {"HEAD_CHARS": ex.HEAD_CHARS, "TAIL_CHARS": ex.TAIL_CHARS, "MAX_AUTHORS": ex.MAX_AUTHORS}}
    selection: dict[str, Any] = (
        {"target_n": n,
         "selection_order": "rows with xml, then clear byline, then the rest; each by sha256(seed + doc_id)"}
        if doc_ids is None else {"doc_ids": list(doc_ids), "selection_order": "the given doc ids; item ids by "
                                 "sha256(seed + doc_id)"})
    if split is not None:
        selection["split_forced"] = split
    manifest = _manifest("affiliation", set_id, src, seed, items, prov, {
        **selection,
        "byline_rule": {"chars": BYLINE_CHARS, "share": BYLINE_SHARE},
        "population": {**src.population, "pool": pool, "by_channel": report["by_channel"]}})
    _write(out, items, manifest, force)
    write_packets(items, packets, AFFILIATION_PACKET, lambda it: {
        "item_id": it["item_id"], "window": it["input"]["window"], "authors_meta": it["input"]["authors_meta"],
        "xml_byline": it["input"]["xml_byline"], "roster_entries": it["input"]["roster_entries"]})
    return items, manifest


def _manifest(task: str, set_id: str, src: Source, seed: str, items: Sequence[Row],
              prov: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    return {"set_id": set_id, "task": task,
            "created_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "source_store": str(src.store), "documents_in": src.documents, "documents_head": src.head,
            **prov, "seed": seed, "order": "sha256(seed + doc_id) hex, ascending",
            "items_sha256": hashlib.sha256(jsonl(items)).hexdigest(),
            "keywords": {"safety_stems": dict(SAFETY_STEMS), "policy": dict(POLICY_TERMS),
                         "governance": dict(GOVERNANCE_TERMS), "governance_weak": dict(GOVERNANCE_WEAK),
                         "hard_body_chars": HARD_BODY_CHARS,
                         "capabilities": dict(CAPABILITY_TERMS), "marketing_words": MARKETING_WORDS,
                         "product_words": PRODUCT_WORDS, "next_to_chars": NEXT_TO_CHARS,
                         "rl_context": RL_CONTEXT},
            **extra, "counts": counts(items)}


def _write(out: Path, items: Sequence[Row], manifest: Mapping[str, Any], force: bool) -> None:
    out.mkdir(parents=True, exist_ok=True)
    target = out / "items.jsonl"
    if target.exists() and not force:
        raise FileExistsError(f"{target} exists: a frozen set is not replaced without --force")
    target.write_bytes(jsonl(items))
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", "utf-8")


# ---------------------------------------------------------------- CLI
app = typer.Typer(add_completion=False, no_args_is_help=True,
                  help="Freeze an LLM eval set from a store's documents head (read-only).")

StoreArg = Annotated[Path, typer.Option("--store", help="Store root (read-only).")]
InArg = Annotated[str, typer.Option("--in", help="The documents version to freeze from: data/documents/<series>/ "
                                    "(its last file) or an exact file; required, no default series.")]
SetArg = Annotated[str, typer.Option("--set", help="Set id, e.g. 2026-10-12a.")]
OutArg = Annotated[Path, typer.Option("--out", help="evals/<task>/<set-id> directory.")]
PacketsArg = Annotated[Path, typer.Option("--packets", help="Labeler packet directory.")]
SeedArg = Annotated[str | None, typer.Option("--seed", help="Sampling seed (default track-e-<set>).")]
ForceArg = Annotated[bool, typer.Option("--force", help="Replace an existing items.jsonl.")]


def _print_counts(c: Mapping[str, Any]) -> None:
    for k in ("total", "split", "channel_group", "channel", "proxy", "hard", "license_class",
              "production_asks", "stratum_split"):
        typer.echo(f"{k}: {json.dumps(c[k], ensure_ascii=False)}")


@app.command()
def triage(store: StoreArg, in_: InArg, set_id: SetArg, out: OutArg, packets: PacketsArg, seed: SeedArg = None,
           force: ForceArg = False) -> None:
    """Freeze the triage set: ~200 items, quotas and hard cases per the module docstring."""
    _, m = freeze("triage", load_source(store.absolute(), in_), set_id, out, packets, seed or f"track-e-{set_id}",
                  force=force)
    typer.echo(f"documents_head: {m['documents_head']}  git: {m['git_commit'][:12]}"
               f"{' (dirty)' if m['git_dirty'] else ''}  items_sha256: {m['items_sha256'][:16]}")
    typer.echo(f"selection: {json.dumps(m['selection'])}")
    _print_counts(m["counts"])


@app.command()
def screen(store: StoreArg, in_: InArg, set_id: SetArg, out: OutArg, packets: PacketsArg, seed: SeedArg = None,
           n: Annotated[int, typer.Option("--n", help="Target set size.")] = SCREEN_N,
           dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the eligible pool and stop.")] = False,
           force: ForceArg = False,
           exclude_set: Annotated[list[Path] | None, typer.Option(
               "--exclude-set", help="items.jsonl of another set: its doc ids are never chosen (repeatable).")] = None,
           prefix: Annotated[str, typer.Option("--prefix", help="Item-id prefix.")] = "scr",
           split_all: Annotated[str | None, typer.Option(
               "--split-all", help="Put every item in this split (e.g. holdout) instead of dev/test.")] = None) -> None:
    """Freeze the screen set (rows with an acquired text); --dry-run prints the pool only."""
    exclude = frozenset(json.loads(line)["doc_id"] for p in exclude_set or [] for line in p.read_text("utf-8").splitlines()
                        if line.strip())
    _, m = freeze_screen(load_source(store.absolute(), in_), set_id, out, packets, seed or f"track-e-{set_id}", n=n,
                         dry_run=dry_run, force=force, exclude=exclude, prefix=prefix, split_all=split_all)
    if dry_run:
        typer.echo(f"documents_head: {m['documents_head']}  live: {m['population']['live']}")
        for k, v in m["pool"].items():
            typer.echo(f"  {k}: {json.dumps(v)}")
        typer.echo(f"pool total: {sum(v['eligible'] for v in m['pool'].values())}  "
                   f"slots (target {n}): {json.dumps(m['quotas'])}")
        return
    typer.echo(f"documents_head: {m['documents_head']}  items_sha256: {m['items_sha256'][:16]}")
    _print_counts(m["counts"])


@app.command()
def affiliation(store: StoreArg, in_: InArg, set_id: SetArg, out: OutArg, packets: PacketsArg, seed: SeedArg = None,
                n: Annotated[int, typer.Option("--n", help="Target set size.")] = AFFILIATION_N,
                dry_run: Annotated[bool, typer.Option("--dry-run", help="Print the pool and stop.")] = False,
                doc_ids: Annotated[str | None, typer.Option(
                    "--doc-ids", help="Comma-separated doc ids: exactly these documents, no sampling.")] = None,
                split: Annotated[str | None, typer.Option(
                    "--split", help="Put every item on this split (dev|test) instead of balancing.")] = None,
                force: ForceArg = False) -> None:
    """Freeze the affiliation set (paper rows with an acquired full text; XML rows first)."""
    ids = None if doc_ids is None else [d.strip() for d in doc_ids.split(",") if d.strip()]
    items, m = freeze_affiliation(load_source(store.absolute(), in_), set_id, out, packets,
                                  seed or f"track-e-{set_id}", n=n, dry_run=dry_run, force=force, doc_ids=ids,
                                  split=split)
    if dry_run:
        typer.echo(f"documents_head: {m['documents_head']}  live: {m['population']['live']}")
        typer.echo(f"pool: {json.dumps(m['pool'])}")
        for k, v in m["by_channel"].items():
            typer.echo(f"  {k}: {json.dumps(v)}")
        return
    typer.echo(f"documents_head: {m['documents_head']}  items_sha256: {m['items_sha256'][:16]}")
    typer.echo(f"pool: {json.dumps(m['population']['pool'])}")
    _print_counts(m["counts"])


if __name__ == "__main__":
    app()
