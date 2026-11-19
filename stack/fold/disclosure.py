"""What the public variant of the dataset release discloses of a document (D808): a per-document quotation budget,
the share of a withheld abstract the quotes may cover, and the redaction of email addresses. Called from
:class:`stack.fold.dataset.Projection`; the numbers are the mapping's ``variants.<variant>.quote_policy``
(``configs/releases/dataset/mapping.yaml``), and a variant without one ships its quotes under the plain cap.

**The budget** (:func:`allocate`, one row). L is the converted text's length in characters as measured at conversion
(the current snapshot's ``quality.metrics.chars``, ``len(text.strip())``); a row without it ships no quotes. A quote
costs the length of the string actually emitted, normalised (:func:`norm`); a quote whose text (or whose emitted form)
was already emitted in the row costs nothing and ships as exactly that retained string. The quotes are grouped by
annotation: the element that holds a quote list or a scalar quote (a technique, a model) or, for the screen and
relevance records, the kind's payload. The annotations are taken records first, then by descending maximum role
strength (``roles``), ties in row order. Pass 1 gives each annotation its shortest quote that fits (ties in order),
cut to ``floor_cut``, under the ceiling min(``ceiling.chars``, ``ceiling.share`` × L). Pass 2 goes round-robin over
the annotations under the budget min(``budget.chars``, ``budget.share`` × L), pass 1's cost included: first each
annotation's floor quote again at the variant's cap (charged the difference; not when another text ships the same
floor string), then its other quotes in their order, at the cap. A quote that does not fit is skipped; the next one is
still offered. Duplicates follow the final retained string.
Where the abstract is withheld, a quote is also refused when it would raise the abstract's covered share above
``abstract_share`` (:class:`AbstractCover`). An omitted quote leaves its list, or becomes ``""`` where it is a required
field of its annotation (the mapping's ``scalar_quotes``). These are controls on how much of a work is quoted, not a
legal threshold.

**Emails** (:func:`redact_emails`): a conventional address (Unicode word characters, ``.+'-`` in the local part, a
dotted domain with an alphabetic top-level label, and the grouped ``{a,b}@domain``) becomes :data:`EMAIL_MARK`. Not an
address: a version pin (``tool@0.1.0``), a URL path's ``@handle``, ``@`` directly after ``/``. A URL with an address in
it (:func:`url_has_email`) is dropped instead. :func:`email_hits` is the independent check over the written files.
"""

from __future__ import annotations

import re
import unicodedata
import urllib.parse
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

EMAIL_MARK = "[email]"
_LOCAL = r"[\w.+'-]+"
#: An address, or the grouped form ``{a,b}@domain``, not preceded by a word character or ``@``.
EMAIL = re.compile(rf"(?<![\w@])(?:\{{{_LOCAL}(?:\s*,\s*{_LOCAL})*\}}|{_LOCAL})@(?:[\w-]+\.)+[^\W\d_]{{2,}}(?![\w-])")
#: The broader detector of :func:`email_hits` (any ``x@y.z``, grouped ``{...}@y.z`` too), and what it allows: a
#: version pin (``@`` and a whole version, ``tool@0.1.0+tierlatexml``), an ``hf://`` URI and, in our own files only,
#: the contact address as a whole token and a prompt pinned to a taxonomy version (``prompt@models-1.1.0``, the
#: stage names release.json and FORMAT.md print).
EMAIL_DETECT = re.compile(r"(?:\{[^{}@\s]+\}|[\w.+'-]+)@[\w-]+(?:\.[\w-]+)+")
CONTACT_ADDRESS = "info@thesafetystack.ai"
_CONTACT = re.compile(r"(?<![\w.+'-])info@thesafetystack\.ai(?![\w-]|\.\w)")
_PIN = re.compile(r"@\d+(?:\.\d+)*(?:[+-][\w.]+)?(?!\w|\.\w)")
_TAXONOMY_PIN = re.compile(r"@(?:techniques|models|orgs)-\d+(?:\.\d+)*(?!\w|\.\w)")
_HF_URI = re.compile(r"hf://\S+")
#: Priority of an annotation that is a kind's whole payload (screen and relevance records): above every role strength.
RECORD_PRIORITY = 5
_WORD = re.compile(r"\w+")
_QUOTE_PATH = re.compile(r"(?:(.+)\.)?(\w+)\[\]\.(\w+)")


def norm(s: str) -> str:
    """``s`` as quotes are compared and charged: NFKC, every whitespace run one space, stripped."""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", s)).strip()


def _words(s: str) -> list[str]:
    return _WORD.findall(norm(s).casefold())


# ---------------------------------------------------------------- the policy
@dataclass(frozen=True)
class QuotePolicy:
    """The mapping's ``variants.<variant>.quote_policy`` (module docstring)."""

    version: int
    floor_cut: int
    budget_chars: int
    budget_share: float
    ceiling_chars: int
    ceiling_share: float
    abstract_share: float
    shingle_words: int

    @classmethod
    def parse(cls, raw: Mapping[str, Any]) -> QuotePolicy:
        want = {"version", "floor_cut", "budget", "ceiling", "abstract_share", "shingle_words"}
        if set(raw) != want or set(raw["budget"]) != {"chars", "share"} or set(raw["ceiling"]) != {"chars", "share"}:
            raise ValueError(f"quote_policy needs exactly {sorted(want)} (budget, ceiling: chars, share); "
                             f"got {dict(raw)}")
        return cls(int(raw["version"]), int(raw["floor_cut"]), int(raw["budget"]["chars"]),
                   float(raw["budget"]["share"]), int(raw["ceiling"]["chars"]), float(raw["ceiling"]["share"]),
                   float(raw["abstract_share"]), int(raw["shingle_words"]))

    def limits(self, length: int) -> tuple[float, float]:
        """(budget, ceiling) of a text of ``length`` characters."""
        return min(self.budget_chars, self.budget_share * length), min(self.ceiling_chars, self.ceiling_share * length)

    def record(self) -> dict[str, Any]:
        """``release.json`` ``policy.quote_policy``'s parameters."""
        return {"version": self.version, "floor_cut": self.floor_cut,
                "budget": {"chars": self.budget_chars, "share": self.budget_share},
                "ceiling": {"chars": self.ceiling_chars, "share": self.ceiling_share},
                "abstract_share": self.abstract_share, "shingle_words": self.shingle_words,
                "length": "the current snapshot's quality.metrics.chars (converted text, code points); none: no quotes",
                "normalisation": "NFKC, whitespace runs to one space, stripped; abstract shingles also case-folded"}


# ---------------------------------------------------------------- the withheld abstract
class AbstractCover:
    """The share of an abstract covered by quotes: the abstract's words (:func:`_words`) that lie in a run of ``n``
    consecutive words also found, in that order, in some quote, by characters (covered words' characters over all
    its words'). Linear: the abstract's ``n``-grams are indexed once. A quote shorter than ``n`` words covers
    nothing."""

    def __init__(self, abstract: str, n: int) -> None:
        words = _words(abstract)
        self.n = n
        self.lens = [len(w) for w in words]
        self.total = sum(self.lens)
        self.index: dict[tuple[str, ...], list[int]] = {}
        for i in range(len(words) - n + 1):
            self.index.setdefault(tuple(words[i:i + n]), []).append(i)
        self.covered: set[int] = set()

    def adding(self, quote: str) -> set[int]:
        """The covered word positions once ``quote`` is emitted too."""
        out = set(self.covered)
        w = _words(quote)
        for j in range(len(w) - self.n + 1):
            for i in self.index.get(tuple(w[j:j + self.n]), ()):
                out.update(range(i, i + self.n))
        return out

    def share(self, covered: set[int] | None = None) -> float:
        cov = self.covered if covered is None else covered
        return sum(self.lens[i] for i in cov) / self.total if self.total else 0.0


# ---------------------------------------------------------------- allocation
@dataclass
class _Quote:
    holder: dict[str, Any]  # the list element, or the annotation of a scalar quote
    key: str
    text: str
    unit: int
    scalar: bool
    emitted: str | None = None


@dataclass
class Allocation:
    """What :func:`allocate` did to one row."""

    budget: float
    ceiling: float
    cost: int = 0  # the normalised characters of the distinct emitted strings
    floor_cost: int = 0  # of which pass 1's
    abstract_share: float | None = None  # the covered share of the withheld abstract, when the rule applied
    quotes: int = 0
    kept: int = 0
    cut: int = 0
    chars_in: int = 0
    chars_out: int = 0
    #: annotations that had a quote and ship none, by ``<kind>`` or ``<kind>.<list>``
    lost: Counter[str] = field(default_factory=Counter)
    #: ``(id(holder), key)`` of each emitted quote -> its length before this policy
    original: dict[tuple[int, str], int] = field(default_factory=dict)


def _units(obj: Any, path: str) -> Iterator[dict[str, Any]]:
    """The dicts at ``path`` (``a[].b``; ``""`` = ``obj``) under ``obj``."""
    if not path:
        if isinstance(obj, dict):
            yield obj
        return
    head, _, rest = path.partition(".")
    v = obj.get(head.removesuffix("[]")) if isinstance(obj, dict) else None
    if v is None:
        return
    for el in (v if head.endswith("[]") else [v]):
        yield from _units(el, rest)


def _priority(unit: Mapping[str, Any], is_record: bool) -> int:
    if is_record:
        return RECORD_PRIORITY
    roles = unit.get("roles")
    return max((int(v or 0) for v in roles.values()), default=0) if isinstance(roles, dict) else 0


def allocate(ann: Mapping[str, Any], quote_paths: Sequence[str], scalar_paths: Sequence[str], policy: QuotePolicy,
             cap: int, length: int | None, abstract: str | None,
             cut: Callable[[str, int], str]) -> Allocation:
    """Apply the budget (module docstring) to the quotes of the release annotations ``ann`` (public payloads, in
    place): ``length`` = L (None: no quotes), ``abstract`` the internal abstract when the public one is withheld
    (None: no abstract rule), ``cap`` pass 2's cut, ``cut`` the release's cut
    (:func:`stack.fold.dataset.cut_string`)."""
    budget, ceiling = policy.limits(length or 0)
    al = Allocation(budget, ceiling)
    cover = AbstractCover(abstract, policy.shingle_words) if abstract else None
    if cover is not None and not cover.total:
        cover = None
    units: list[tuple[int, int, str]] = []  # (priority, row order, lost label)
    quotes: list[_Quote] = []
    lists: list[tuple[dict[str, Any], str]] = []  # (annotation, quote list key)
    for p in quote_paths:
        m = re.fullmatch(r"annotations\.(\w+)\.payload\.(.+)", p)
        if m is None:
            raise ValueError(f"quote path {p!r} is not annotations.<kind>.payload.<path>")
        kind, rest = m.groups()
        v = ann.get(kind)
        if v is None or v.get("payload") is None:
            continue
        if p in scalar_paths:
            upath, lst, leaf = rest.rpartition(".")[0], None, rest.rpartition(".")[2]
        else:
            qm = _QUOTE_PATH.fullmatch(rest)
            if qm is None:
                raise ValueError(f"quote path {p!r}: not <annotation>.<list>[].<leaf> (or listed in scalar_quotes)")
            upath, lst, leaf = qm.group(1) or "", qm.group(2), qm.group(3)
        label = kind + (f".{upath.split('.')[-1].removesuffix('[]')}" if upath else "")
        for u in _units(v["payload"], upath):
            elems = [u] if lst is None else [q for q in u.get(lst) or [] if isinstance(q, dict)]
            if lst is not None:
                lists.append((u, lst))
            ui = len(units)
            n0 = len(quotes)
            for q in elems:
                if isinstance(q.get(leaf), str) and q[leaf]:
                    quotes.append(_Quote(q, leaf, q[leaf], ui, lst is None))
            if len(quotes) > n0:
                units.append((_priority(u, not upath), ui, label))
    order = sorted(range(len(units)), key=lambda i: (-units[i][0], units[i][1]))
    by_unit: dict[int, list[_Quote]] = {i: [] for i in order}
    for q in quotes:
        by_unit[q.unit].append(q)
    retained: dict[str, str] = {}  # norm(original text) -> the string it ships as (final after an upgrade)
    users: dict[str, set[str]] = {}  # norm(emitted string) -> the norm(original)s that ship it
    canon: dict[str, str] = {}  # norm(emitted string) -> the one string shipped for it
    kept: set[int] = set()  # id of each kept _Quote

    def ship(q: _Quote, out: str) -> None:
        out = canon.setdefault(norm(out), out)
        retained[norm(q.text)] = out
        users.setdefault(norm(out), set()).add(norm(q.text))
        kept.add(id(q))

    def free(q: _Quote, out: str) -> bool:
        if norm(q.text) in retained:  # the same text: it follows the retained string
            kept.add(id(q))
            return True
        if norm(out) in users:  # another text emitted the same string: ship that one
            ship(q, out)
            return True
        return False

    def fits(extra: int, limit: float, out: str) -> bool:
        if al.cost + extra > limit:
            return False
        if cover is not None:
            cov = cover.adding(out)
            if cover.share(cov) > policy.abstract_share:
                return False
            cover.covered = cov
        al.cost += extra
        return True

    def offer(q: _Quote, width: int, limit: float) -> bool:
        out = cut(q.text, width)
        if free(q, out):
            return True
        if not fits(len(norm(out)), limit, out):
            return False
        ship(q, out)
        return True

    def upgrade(q: _Quote) -> None:
        """A floor quote again at the cap, charged the difference; not when another text ships its floor string."""
        old, new = retained[norm(q.text)], cut(q.text, cap)
        if norm(new) == norm(old) or users[norm(old)] != {norm(q.text)}:
            return
        if fits((0 if norm(new) in users else len(norm(new))) - len(norm(old)), budget, new):
            del users[norm(old)], canon[norm(old)]
            ship(q, new)

    floors: dict[int, _Quote] = {}
    if length:
        for ui in order:  # pass 1: per annotation its shortest quote that fits, cut to floor_cut
            for q in sorted(by_unit[ui], key=lambda q: len(q.text)):
                if offer(q, policy.floor_cut, ceiling):
                    floors[ui] = q
                    break
        al.floor_cost = al.cost
        # pass 2, round-robin under the budget: each annotation's floor quote again at the cap, then its other quotes
        turns = [([(True, floors[ui])] if ui in floors else []) + [(False, q) for q in by_unit[ui] if id(q) not in kept]
                 for ui in order]
        for r in range(max(map(len, turns), default=0)):
            for t in turns:
                if r < len(t):
                    is_up, q = t[r]
                    if is_up:
                        upgrade(q)
                    elif id(q) not in kept:
                        offer(q, cap, budget)
        for q in quotes:  # a later duplicate of a quote kept in pass 2
            if id(q) not in kept:
                free(q, cut(q.text, cap))
    for q in quotes:
        if id(q) in kept:
            q.emitted = retained[norm(q.text)]
    dropped: set[int] = set()
    al.quotes = len(quotes)
    for q in quotes:
        al.chars_in += len(q.text)
        if q.emitted is None:
            if q.scalar:
                q.holder[q.key] = ""  # a required field: the annotation stays
            else:
                dropped.add(id(q.holder))
            continue
        al.kept += 1
        al.cut += len(q.emitted) < len(q.text)
        al.chars_out += len(q.emitted)
        al.original[(id(q.holder), q.key)] = len(q.text)
        q.holder[q.key] = q.emitted
    for u, lst in lists:
        if u.get(lst):
            u[lst] = [q for q in u[lst] if id(q) not in dropped]
    for i, (_p, _o, label) in enumerate(units):
        if all(q.emitted is None for q in by_unit[i]):
            al.lost[label] += 1
    if cover is not None:
        al.abstract_share = cover.share()
    return al


# ---------------------------------------------------------------- emails
def redact_emails(s: str) -> tuple[str, int]:
    """``s`` with every address (module docstring) replaced by :data:`EMAIL_MARK`, and how many."""
    return EMAIL.subn(EMAIL_MARK, s)


def url_has_email(url: str) -> bool:
    """A ``mailto:`` URL, or one with an address in its user info, path, query or fragment (percent-decoded; URL
    punctuation read as spaces, so ``/people/a@b.org`` counts; a grouped ``{a,b}@c.org`` keeps its syntax). Also
    for an identifier that holds a URL (an address in one refuses the export)."""
    if url[:7].lower() == "mailto:":
        return True
    try:
        sp = urllib.parse.urlsplit(url)
    except ValueError:
        return bool(EMAIL.search(urllib.parse.unquote(url)))
    user = sp.netloc.rpartition("@")[0]
    parts = " ".join(urllib.parse.unquote_plus(x) for x in (user, sp.path, sp.query, sp.fragment))
    return bool(EMAIL.search(re.sub(r"[/?#&=:;]", " ", parts)))


def email_hits(text: str, *, ours: bool) -> list[str]:
    """The independent check (D808): what :data:`EMAIL_DETECT` finds in ``text``, less a version pin, an ``hf://``
    URI and, in ``ours`` (our own files, not the corpus), :data:`CONTACT_ADDRESS` and a taxonomy pin."""
    text = _HF_URI.sub(" ", text)
    if ours:
        text = _CONTACT.sub(" ", text)
    out = []
    for m in EMAIL_DETECT.finditer(text):
        at = m.start() + m.group(0).index("@")
        if not (_PIN.match(text, at) or ours and _TAXONOMY_PIN.match(text, at)):
            out.append(m.group(0))
    return out


def string_leaves(obj: Any, path: str = "") -> Iterator[tuple[str, Any, Any, str]]:
    """Every string under ``obj`` as ``(path without indices, holder, key, value)``; ``holder[key] = new`` replaces
    it. Lists add ``[]`` to the path."""
    if isinstance(obj, dict):
        items: Iterator[tuple[Any, Any]] = iter(obj.items())
    elif isinstance(obj, list):
        items = enumerate(obj)
    else:
        return
    for k, v in items:
        p = (f"{path}.{k}" if path else str(k)) if isinstance(obj, dict) else f"{path}[]"
        if isinstance(v, str):
            yield p, obj, k, v
        else:
            yield from string_leaves(v, p)
