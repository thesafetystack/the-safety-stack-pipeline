"""The model-mention matcher of ``annotate.model_roles`` (LLM-free; ``configs/models.yaml``).

Moved here from ``workflows/annotate/model_mentions/main.py`` when ``annotate.model_mentions`` merged
into ``annotate.model_roles`` (D591; the matcher itself unchanged): ``main.ask`` scans the document with
it, builds the judge's menu from the strong and weak hits and stores every hit in the payload's
``matches`` / ``ambiguous``. ``stack eval model_mentions`` scores it alone (no LLM). Loaded by path
(``workflows/`` is not a package): ``main`` through ``stack.stage_runner.load_path_module`` (registered
in ``sys.modules``, D435), the eval as ``workflows.annotate.model_roles.match``.

The vocabulary's typed shape and loader are :mod:`stack.modelvocab`. Everything above the
"── beyond the twin ──" divider is the matcher core (v3's, verbatim but for ``compile_vocab``);
below it: strong-vs-weak classification, the prefilter, :class:`Scanner`, and the payload helpers
(:func:`scan_payload`, :func:`payload_fields`, :func:`resolve_name`) and the per-process scanner cache
(:func:`scanner_for`).

Imports ``stack.*`` only, never another workflow.
"""

import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple

from stack.modelvocab import Vocabulary, load_vocab

# ═══════════════════════════════════════════════════════════════════════════════════════
# MATCHER — pattern compiler + overlap resolution over the model vocabulary.
# ═══════════════════════════════════════════════════════════════════════════════════════
#
# Implements the mention-matching design of ``doc/model-collection/schema.md`` (v0.3):
# - patterns are canonical token sequences; tokens split on ``[\s\-_./:]+``
# - joins: optional separator class ``{0,3}``; digit-digit joins require ``{1,3}``
# - boundary guards ``(?<![a-z0-9]) … (?![a-z0-9])``; case-insensitive
# - resolution among overlapping matches: longer span > deeper node > ambiguous
#
# ORIGIN: the matcher core below (SEP_CLASS … resolve) was ported from v3's vocabulary-dev tool
# ``doc/model-collection/vocab_tool.py`` (branch ``tomas/model-roles``, not in v4). v3 is retired,
# so there is no twin to keep in sync; the core stays as it is so the clustering (which mention
# wins an overlap) does not change. The fixture suite (copied to ``tests/fixtures/model_mentions/``)
# pins that behaviour: re-run ``tests/test_annotate_model_roles_match.py`` after any change here.
#
# The matcher deliberately consumes the RAW YAML mapping (``dict[str, Any]`` per node), not
# ``stack.modelvocab.ModelVocab`` (v3: ``stack.types.vocab``). Two reasons: the core above keeps
# v3's raw-mapping interface unchanged, and the pydantic layer is a file validator rather than the runtime carrier. Use
# ``stack.modelvocab.load_vocab`` to get a validated + stamped vocabulary carrying the raw
# mapping the matcher wants.
#
# Everything below the "── beyond the twin ──" divider is NEW here and has no counterpart in
# vocab_tool.py: strong-vs-weak classification of a resolved span, the substring prefilter that
# makes a corpus-wide sweep tractable, and ``Scanner`` — the one per-document scan.

# ═══════════════════════════════════════════════════════════════════════════════════════
# MATCHER CORE — as ported from v3's vocab_tool.py; kept as is (clustering behaviour, above).
# ═══════════════════════════════════════════════════════════════════════════════════════

# separators include Unicode hyphens/dashes (U+2010–U+2015, U+2212) and NBSP —
# publisher sites emit non-breaking hyphens inside model names ("GPT‑5.1")
SEP_CLASS = r"[ \t\-_./: ‐-―−]"
TOKEN_SPLIT = re.compile(r"[\s\-_./:‐-―−]+")


def compile_pattern(pattern: str, verbatim: bool = False) -> str:
    """Compile one canonical pattern to a regexp fragment (no guards)."""
    if verbatim:
        return re.escape(pattern)
    tokens = [t for t in TOKEN_SPLIT.split(pattern.strip().lower()) if t]
    if not tokens:
        raise ValueError(f"empty pattern: {pattern!r}")
    parts = [re.escape(tokens[0])]
    for prev, tok in zip(tokens, tokens[1:]):
        required = prev[-1].isdigit() and tok[0].isdigit()
        parts.append(SEP_CLASS + ("{1,3}" if required else "{0,3}"))
        parts.append(re.escape(tok))
    return "".join(parts)


def compile_node_regex(node: dict[str, Any]) -> re.Pattern[str] | None:
    """Full compiled regexp for a node (strong + weak + verbatim), or None.

    Patterns are ordered longest-first: Python alternation is first-match, and a
    shorter alternative listed earlier would otherwise truncate a longer match.
    """
    pats = [(p, False) for p in node.get("mentions", []) or []]
    pats += [(p, False) for p in node.get("weak_mentions", []) or []]
    pats += [(p, True) for p in node.get("mentions_verbatim", []) or []]
    if not pats:
        return None
    pats.sort(key=lambda pv: len(pv[0]), reverse=True)
    body = "|".join(f"(?:{compile_pattern(p, verbatim=v)})" for p, v in pats)
    return re.compile(rf"(?<![a-z0-9])(?:{body})(?![a-z0-9])", re.IGNORECASE)


def is_ancestor(nodes: dict[str, Any], a: str, b: str) -> bool:
    """True if node a is an ancestor of node b."""
    cur, seen = nodes.get(b, {}).get("parent"), set()
    while cur and cur not in seen:
        if cur == a:
            return True
        seen.add(cur)
        cur = nodes.get(cur, {}).get("parent")
    return False


def resolve(
    nodes: dict[str, Any], regexes: dict[str, re.Pattern[str]], text: str
) -> list[dict[str, Any]]:
    """All resolved mentions in text: overlap clusters -> longest span, then deepest."""
    matches: list[dict[str, Any]] = []
    for nid, rx in regexes.items():
        for mo in rx.finditer(text):
            matches.append({"node": nid, "start": mo.start(), "end": mo.end()})
    return _cluster(nodes, matches)


def _cluster(nodes: dict[str, Any], matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The second half of the twin's ``resolve``, split out so the prefiltered fast path
    (:meth:`Prefilter.resolve`) can reuse it instead of duplicating the resolution rule.

    THE THREE DELTAS FROM THE TWIN, all mechanical, none semantic: this split; the
    ``matches``/``resolved`` type annotations and the ``mo`` loop variable above (required by
    ``mypy --strict``, which this file is kept clean under — run it by hand, ``pyproject``
    scopes mypy to ``stack/``; the twin reuses ``m`` for both the Match and the match dict).
    The loop body below is byte-identical to the twin's.
    """
    matches.sort(key=lambda m: (m["start"], -(m["end"] - m["start"])))
    resolved: list[dict[str, Any]] = []
    for m in matches:
        overlapping = [r for r in resolved if r["start"] < m["end"] and m["start"] < r["end"]]
        if not overlapping:
            resolved.append({**m, "candidates": [m["node"]]})
            continue
        for r in overlapping:
            r_len, m_len = r["end"] - r["start"], m["end"] - m["start"]
            if m_len > r_len:
                r.update(node=m["node"], start=m["start"], end=m["end"], candidates=[m["node"]])
            elif m_len == r_len:
                # equal length: within one lineage the deeper node wins regardless
                # of offset ("tii falcon" family vs "falcon 40b" model overlapping
                # in "TII Falcon 40B"); cross-lineage same-start is true ambiguity;
                # cross-lineage different-start keeps the leftmost.
                if is_ancestor(nodes, r["node"], m["node"]):
                    r.update(node=m["node"], start=m["start"], end=m["end"],
                             candidates=[m["node"]])
                elif is_ancestor(nodes, m["node"], r["node"]):
                    pass  # r is the deeper one already
                elif m["start"] == r["start"] and m["node"] not in r["candidates"]:
                    r["candidates"].append(m["node"])  # true ambiguity
            # shorter overlapping match: dropped
    return resolved


# ═══════════════════════════════════════════════════════════════════════════════════════
# ── beyond the twin ── NEW here; no counterpart in vocab_tool.py.
# ═══════════════════════════════════════════════════════════════════════════════════════


def compile_vocab(
    nodes: dict[str, Any],
) -> tuple[dict[str, re.Pattern[str]], dict[str, re.Pattern[str]], "Prefilter"]:
    """Compile once: ``(regexes, strong_regexes, prefilter)`` (v3 ``Vocabulary.compiled``)."""
    regexes = compile_regexes(nodes)
    return regexes, compile_strong_regexes(nodes), Prefilter(nodes, regexes)


def compile_regexes(nodes: dict[str, Any]) -> dict[str, re.Pattern[str]]:
    """Every node's combined regex, keyed by id. Nodes with no patterns are absent."""
    return {nid: rx for nid, n in nodes.items() if (rx := compile_node_regex(n))}


def compile_strong_regexes(nodes: dict[str, Any]) -> dict[str, re.Pattern[str]]:
    """Per node, the regex built from ``mentions`` + ``mentions_verbatim`` ONLY.

    ``compile_node_regex`` merges strong and weak patterns into one alternation, so a
    resolved hit does not say which kind of pattern produced it — and that distinction is
    exactly what a corpus sweep needs (bare "opus"/"nova"/"bert" are context-needed hits,
    not model mentions). :func:`is_strong_span` recovers it by re-testing the matched span
    against this strong-only regex; because every resolved span IS one alternative of the
    combined regex (both are guard-anchored), a full match here means the winning
    alternative was a strong one.
    """
    out: dict[str, re.Pattern[str]] = {}
    for nid, n in nodes.items():
        strong = {k: v for k, v in n.items() if k != "weak_mentions"}
        if (rx := compile_node_regex(strong)) is not None:
            out[nid] = rx
    return out


def is_strong_span(strong_regexes: dict[str, re.Pattern[str]], nid: str, span: str) -> bool:
    """Did node ``nid``'s hit on ``span`` come from a strong pattern rather than a weak one?"""
    rx = strong_regexes.get(nid)
    return rx is not None and rx.fullmatch(span) is not None


def first_tokens(node: dict[str, Any]) -> set[str] | None:
    """Lowercased leading token of every pattern on the node — the prefilter keys.

    ``None`` means "cannot be prefiltered, always run this node's regex": a verbatim
    pattern that does not begin with an alphanumeric run (Midjourney's ``--v 6``) has no
    usable literal prefix.

    Soundness: the first token is compiled as ``re.escape(token)`` at the very start of its
    alternative under ``re.IGNORECASE``, so if the node's regex matches anywhere in a text,
    the lowercased text necessarily CONTAINS that lowercased token as a plain substring.
    The prefilter can therefore only ever skip nodes that would not have matched. (Verbatim
    patterns are escaped whole, so their leading alnum run is a literal prefix likewise.)
    """
    toks: set[str] = set()
    for field in ("mentions", "weak_mentions"):
        for p in node.get(field, []) or []:
            parts = [t for t in TOKEN_SPLIT.split(p.strip().lower()) if t]
            if not parts:
                continue
            toks.add(parts[0])
    for p in node.get("mentions_verbatim", []) or []:
        m = re.match(r"[0-9a-z]+", p.strip().lower())
        if m is None:
            return None  # no literal prefix to test — never prefilter this node out
        toks.add(m.group(0))
    return toks


class Prefilter:
    """Cheap per-document narrowing of the ~1,000 node regexes down to the plausible ones.

    Built once per vocabulary. Two uses, both keyed on the same first-token index:

    :meth:`nodes_for`   which nodes could match this document at all (lowercase once, one
                        ``str.find`` per DISTINCT first token);
    :meth:`resolve`     the same thing at POSITION granularity — the sweep's hot loop.

    See :func:`first_tokens` for why neither can change the result set;
    ``tests/test_annotate_model_roles_match.py`` asserts the equivalence against the plain-``finditer`` path on
    real corpus documents.
    """

    def __init__(self, nodes: dict[str, Any], regexes: dict[str, re.Pattern[str]]) -> None:
        self.by_token: dict[str, list[str]] = {}
        self.always: list[str] = []
        for nid in regexes:
            toks = first_tokens(nodes[nid])
            if toks is None or not toks:
                self.always.append(nid)
                continue
            for t in toks:
                self.by_token.setdefault(t, []).append(nid)

    def nodes_for(self, text: str) -> list[str]:
        lowered = text.lower()
        hit = set(self.always)
        for tok, nids in self.by_token.items():
            if lowered.find(tok) >= 0:
                hit.update(nids)
        return sorted(hit)

    def _starts(self, text: str) -> dict[str, list[int]] | None:
        """node id → the ONLY offsets its regex can match at, ascending. None = cannot tell.

        ``None`` means the lowercase mapping is not length-preserving for this text (Turkish
        dotted capital I lowercases to two code points), so offsets computed on the lowered
        copy would be shifted — the caller must fall back to the unfiltered path rather than
        silently lose matches.
        """
        lowered = text.lower()
        if len(lowered) != len(text):
            return None
        starts: dict[str, list[int]] = {}
        for tok, nids in self.by_token.items():
            i = lowered.find(tok)
            if i < 0:
                continue
            positions = []
            while i >= 0:
                positions.append(i)
                i = lowered.find(tok, i + 1)
            for nid in nids:
                starts.setdefault(nid, []).extend(positions)
        return starts

    def resolve(
        self, nodes: dict[str, Any], regexes: dict[str, re.Pattern[str]], text: str
    ) -> list[dict[str, Any]]:
        """:func:`resolve`, but only attempting each node's regex where it could possibly hit.

        A node's regex can only match at an offset where one of its patterns' first tokens
        literally occurs (:func:`first_tokens`), so ``rx.match(text, p)`` over those offsets
        — skipping any offset inside the previous match, which is how ``finditer`` resumes —
        reproduces ``rx.finditer(text)`` exactly, while attempting a few hundred positions
        instead of every position in a 100 kB document. ``rx.match(text, pos)`` keeps the
        leading ``(?<![a-z0-9])`` guard honest: a lookbehind still sees the characters BEFORE
        ``pos`` (verified: ``'sgpt-4'`` does not match at offset 1).

        This is the sweep's ~10x: the unfiltered path spends ~740 ms/doc on ~250 live node
        regexes over a ~100 kB document, essentially all of it in ``finditer``.
        """
        starts = self._starts(text)
        if starts is None:
            return resolve(nodes, regexes, text)
        matches: list[dict[str, Any]] = []
        for nid, positions in starts.items():
            rx = regexes[nid]
            end = 0
            for p in sorted(set(positions)):
                if p < end:
                    continue  # finditer resumes at the end of the previous match
                if (mo := rx.match(text, p)) is not None:
                    matches.append({"node": nid, "start": p, "end": mo.end()})
                    end = mo.end()
        for nid in self.always:  # no literal prefix to index on (Midjourney's `--v 6`)
            for mo in regexes[nid].finditer(text):
                matches.append({"node": nid, "start": mo.start(), "end": mo.end()})
        return _cluster(nodes, matches)


class Scanner:
    """The compiled vocabulary. Build once, :meth:`scan` many documents.

    THE one scan implementation: ``main.ask`` (the menu and the payload's ``matches``) and
    ``stack eval model_mentions`` are its callers — a second copy anywhere would be a second
    answer to "which models does this document mention".

    Pure: no IO, no globals, no state mutated by :meth:`scan`.
    """

    def __init__(self, vocab: Vocabulary, prefilter: bool = True) -> None:
        self.nodes = vocab.nodes
        self.regexes, self.strong_regexes, self._pf = compile_vocab(vocab.nodes)
        self.prefilter = prefilter

    def scan(self, text: str) -> dict[str, Any]:
        """Resolve every model mention in ``text`` → the three buckets.

        ``mentions``   hits whose winning pattern came from the node's ``mentions`` /
                       ``mentions_verbatim`` list — a real, unambiguous model mention;
        ``weak``       hits whose winning pattern came from ``weak_mentions`` — collision-prone
                       bare forms ("opus", "nova", "bert"). Schema v0.3 leaves the context
                       policy to the analysis stage, so this separates them and resolves nothing;
        ``ambiguous``  spans the matcher could not attribute to one node (equal-length,
                       cross-lineage matches — "nova": Amazon vs Deepgram), with ALL candidates.
                       Inventing a winner here would be exactly the information loss the schema
                       forbids. Each entry carries ``strength``, the strong/weak split the other
                       two buckets get from their key: an ambiguous span belongs to no bucket, so
                       without it a real cross-lineage collision between two model names is
                       indistinguishable from the far commoner case of several nodes listing the
                       same collision-prone weak form. ``"strong"`` iff ANY tying candidate
                       matched this span via a strong pattern — a span one candidate names
                       outright is a strong ambiguity even if the others only brush it weakly;
                       requiring all of them would let one weak co-lister mask a real mention.

        Duplicates are kept everywhere: the value lists are the occurrence records, in document
        order, holding the string that actually appeared in the text — so the list length IS the
        occurrence count.
        """
        hits = (self._pf.resolve(self.nodes, self.regexes, text) if self.prefilter
                else resolve(self.nodes, self.regexes, text))
        mentions: dict[str, list[str]] = defaultdict(list)
        weak: dict[str, list[str]] = defaultdict(list)
        ambiguous: list[dict[str, Any]] = []
        for hit in sorted(hits, key=lambda h: h["start"]):
            span = text[hit["start"]:hit["end"]]
            if len(hit["candidates"]) > 1:
                # Every tying candidate matched THIS span (``_cluster`` only accumulates
                # candidates on equal-length, same-start matches, and resets the list whenever
                # the winner's span moves), so ``is_strong_span`` — the same test the single-
                # candidate branch below uses — is well-defined for each of them.
                strong = any(
                    is_strong_span(self.strong_regexes, nid, span) for nid in hit["candidates"]
                )
                ambiguous.append({
                    "text": span,
                    "candidates": sorted(hit["candidates"]),
                    "strength": "strong" if strong else "weak",
                })
                continue
            nid = hit["candidates"][0]
            bucket = mentions if is_strong_span(self.strong_regexes, nid, span) else weak
            bucket[nid].append(span)
        return {"mentions": dict(mentions), "weak": dict(weak), "ambiguous": ambiguous}


# ═══════════════════════════════════════════════════════════════════════════════════════
# PAYLOAD HELPERS AND THE SCANNER CACHE
# ═══════════════════════════════════════════════════════════════════════════════════════


def scan_payload(scanner: Scanner, text: str) -> dict[str, Any]:
    """The scan of one text as lists (ex-``MODEL_MENTIONS``, the shape ``stack eval model_mentions``
    and the frozen eval sets use): ``mentions`` and ``weak`` as ``{node_id, surfaces}`` sorted by
    ``node_id`` (surfaces in document order, duplicates kept), ``ambiguous`` in document order. Pure."""
    out = scanner.scan(text)
    return {
        "mentions": [{"node_id": n, "surfaces": list(s)} for n, s in sorted(out["mentions"].items())],
        "weak": [{"node_id": n, "surfaces": list(s)} for n, s in sorted(out["weak"].items())],
        "ambiguous": [dict(a) for a in out["ambiguous"]],
    }


def is_empty(scan: dict[str, Any]) -> bool:
    """No mention, weak mention or ambiguous span (a :meth:`Scanner.scan` or :func:`scan_payload` result)."""
    return not (scan["mentions"] or scan["weak"] or scan["ambiguous"])


def payload_fields(scan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """The ``MODEL_ROLES`` fields of one :meth:`Scanner.scan` result: ``matches`` = every strong
    entry ``{node_id, surfaces, strength: "strong"}`` sorted by ``node_id``, then every weak one
    (``strength: "weak"``; a node with both strong and weak hits has one entry of each), and
    ``ambiguous`` as the scan has it (document order)."""
    matches = [{"node_id": n, "surfaces": list(s), "strength": strength}
               for pool, strength in (("mentions", "strong"), ("weak", "weak"))
               for n, s in sorted(scan[pool].items())]
    return {"matches": matches, "ambiguous": [dict(a) for a in scan["ambiguous"]]}


def resolve_name(scanner: Scanner, name: str) -> str | None:
    """The vocabulary node a model name stands for (an ``extra_models[].name_as_written``): the one
    node of its strong hits, when it has exactly one and no strong ambiguous span; else ``None``
    (no hit, weak hits only, two nodes, a strong collision)."""
    out = scanner.scan(name)
    if len(out["mentions"]) != 1 or any(a["strength"] == "strong" for a in out["ambiguous"]):
        return None
    return next(iter(out["mentions"]))


class Compiled(NamedTuple):
    """A loaded vocabulary and its compiled :class:`Scanner`."""

    vocab: Vocabulary
    scanner: Scanner


#: One compiled scanner per vocabulary digest (compiling ~1,200 node regexes takes about a second).
_SCANNERS: dict[str, Scanner] = {}
_LOCK = threading.Lock()


def scanner_for(config_dir: Path | None) -> Compiled:
    """The vocabulary in ``config_dir`` (:func:`stack.modelvocab.load_vocab`: read, hashed and
    validated, cached by path and digest) and its scanner, compiled once per digest per process
    (thread-safe: the stage's workers share it)."""
    vocab = load_vocab(config_dir)
    with _LOCK:
        if vocab.digest not in _SCANNERS:
            _SCANNERS[vocab.digest] = Scanner(vocab)
        return Compiled(vocab, _SCANNERS[vocab.digest])


__all__ = ["Compiled", "Prefilter", "Scanner", "compile_node_regex", "compile_pattern", "compile_regexes",
           "compile_strong_regexes", "compile_vocab", "first_tokens", "is_ancestor", "is_empty", "is_strong_span",
           "payload_fields", "resolve", "resolve_name", "scan_payload", "scanner_for"]
