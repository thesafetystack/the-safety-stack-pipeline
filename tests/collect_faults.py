"""Fault injection for the collect phase — the harness ``test_collect_robustness.py`` drives.

WHAT THIS IS FOR. Collect's coverage receipt (``coverage.exhausted``) is the corpus's only claim
about its own completeness, and it is the one field whose corruption is PERMANENT: a query that
banks ``complete`` for a page it never actually read advances the watermark past work that will
then never be collected again. Every other collect error is retryable; this one silently strands
the backlog. The store's schema checks enforce that the value is a member of the enum
(SHOULD-14 — "silence must not claim completeness"), but nothing enforces that it is TRUE.

Truth is a property of the pagination loops, and pagination only misbehaves when a page fails
mid-sweep — the case a hand-written fixture never exercises, because a fixture that returns pages
always returns them. So: wrap the transport, let the first ``fail_at - 1`` calls through, and
break the next one. Then assert on what got banked.

The wrappers are deliberately thin and substrate-free — no DB, no blobs, no network — so a
robustness test over five families and a dozen failure points still runs in under a second.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

# The failure modes that actually happen to us in the field, rather than a generic 500:
#   http_429      OpenAlex outside the polite pool, and arXiv (which IP-blocked this host once)
#   http_500      any upstream having a bad day
#   timeout       the transport raising rather than returning a status
#   truncated     a body that stops mid-document — the JSON/XML parse fails, not the request
#   garbage       a 200 whose body is not the document type at all (a proxy's block page)
MODES = ("http_429", "http_500", "timeout", "truncated", "garbage")


def _fault_json(mode: str) -> tuple[int, Any]:
    """The ``(status, data)`` a BROKEN ``http_get_json`` / ``http_post`` really produces.

    Faithfulness matters more here than coverage. Every one of these transports documents that it
    NEVER raises — a transport failure is ``(0, None)`` and an unparseable body is
    ``(status, None)``, precisely so the caller can bank an honest receipt instead of crashing the
    sweep. A stub that raised instead would be testing a code path that cannot occur, and would
    fail the sweep for a reason production never sees.

    ``truncated`` and ``garbage`` therefore converge on the same value for a JSON endpoint: a body
    that stops mid-object and a proxy's HTML block page both come back from ``json.loads`` as a
    ``JSONDecodeError``, which these functions turn into ``(200, None)``. They stay separate modes
    because they do NOT converge for arXiv's Atom (see ``_fault_text``).
    """
    if mode == "http_429":
        return 429, None
    if mode == "http_500":
        return 500, None
    if mode == "timeout":
        return 0, None                     # urlopen raised; caught and reported as status 0
    return 200, None                       # truncated / garbage: body did not parse


def _fault_text(mode: str) -> tuple[int, str]:
    """The ``(status, body)`` a broken ``http_get`` really produces (arXiv's Atom feed).

    Unlike the JSON case the two malformed-body modes stay distinct, because the sweep parses the
    text itself: ``truncated`` is well-formed-so-far XML that ends mid-element, ``garbage`` is a
    complete HTML document that is not a feed at all. Both must reach ``parse_entries`` and come
    back ``None``; a stub returning an empty string would instead look like a legitimate
    end-of-results and mask the very bug this file hunts.
    """
    if mode == "http_429":
        return 429, ""
    if mode == "http_500":
        return 500, ""
    if mode == "timeout":
        return 0, ""
    if mode == "truncated":
        return 200, '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry>'
    return 200, "<html><body>403 Forbidden: your proxy said no</body></html>"


class FaultyCall:
    """Wraps one module-level ``http_*`` callable; breaks calls from ``fail_at`` (1-indexed).

    ``kind`` selects the shape the wrapped function is contracted to return — ``"json"`` for the
    ``(status, data)`` pairs OpenAlex/S2/forums/AF use, ``"text"`` for arXiv's ``(status, xml)``.

    ``fail_for`` is how many CONSECUTIVE calls fail, and it defaults to 0, meaning "every call
    from ``fail_at`` onward". That default is the important one. OpenAlex and S2 wrap each page in
    a retry ladder (``RETRY_DELAYS_S``), so a single injected 429 is retried and recovers — which
    tests resilience, not honesty. To ask the question this file exists to ask ("what does the
    loop bank when the page is genuinely unavailable?") the failure has to outlive the retries.
    Set ``fail_for=1`` deliberately when the retry-recovers path IS the subject.
    """

    def __init__(
        self, inner: Callable[..., Any], *, fail_at: int, mode: str, kind: str,
        fail_for: int = 0,
    ) -> None:
        assert mode in MODES, f"unknown mode {mode!r}"
        assert kind in ("json", "text")
        self.inner = inner
        self.fail_at = fail_at
        self.fail_for = fail_for
        self.mode = mode
        self.kind = kind
        self.calls = 0
        self.failed = False

    def _is_failing(self) -> bool:
        if self.calls < self.fail_at:
            return False
        return self.fail_for == 0 or self.calls < self.fail_at + self.fail_for

    def __call__(self, *a: Any, **kw: Any) -> Any:
        self.calls += 1
        if self._is_failing():
            self.failed = True
            return _fault_json(self.mode) if self.kind == "json" else _fault_text(self.mode)
        return self.inner(*a, **kw)


class ScriptedPages:
    """A canned paginator serving ``pages`` in order, then an empty page.

    The healthy baseline a fault run is compared against — without it "banked nothing" proves
    nothing, because a loop that banks nothing unconditionally would also pass.

    The key names differ per upstream (OpenAlex pages on ``results`` + ``meta.next_cursor``, S2 on
    ``data`` + ``token``), so they are parameters rather than assumptions; getting them wrong
    would make the loop see one empty page and report a perfectly honest ``complete``, and the
    test would pass for the wrong reason.
    """

    def __init__(
        self, pages: list[list[dict[str, Any]]], *,
        items_key: str = "data", next_key: str = "token",
    ) -> None:
        self.pages = pages
        self.items_key = items_key
        self.next_key = next_key
        self.i = 0

    def json_page(self, *a: Any, **kw: Any) -> tuple[int, Any]:
        i = self.i
        if i >= len(self.pages):
            return 200, {self.items_key: [], self.next_key: None}
        self.i += 1
        more = i + 1 < len(self.pages)
        body: dict[str, Any] = {self.items_key: self.pages[i]}
        if self.next_key == "meta.next_cursor":
            body["meta"] = {"next_cursor": "cur" if more else None}
        else:
            body[self.next_key] = "tok" if more else None
        return 200, body


def openalex_pages(pages: list[list[dict[str, Any]]]) -> ScriptedPages:
    return ScriptedPages(pages, items_key="results", next_key="meta.next_cursor")


def s2_pages(pages: list[list[dict[str, Any]]]) -> ScriptedPages:
    return ScriptedPages(pages, items_key="data", next_key="token")


def atom_feed(entries: list[dict[str, str]], total: int) -> str:
    """A minimal but REAL arXiv Atom document — parsed by the sweep's own ``parse_entries``, so a
    change to that parser is felt here rather than silently bypassed by a stub."""
    items = "".join(
        f'<entry><id>http://arxiv.org/abs/{e["id"]}v1</id>'
        f'<title>{e.get("title", "T")}</title><summary>{e.get("summary", "S" * 60)}</summary>'
        f'<published>{e.get("published", "2024-01-02T00:00:00Z")}</published>'
        f'<author><name>{e.get("author", "A Person")}</name></author>'
        f'<category term="cs.LG"/></entry>'
        for e in entries
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns="http://www.w3.org/2005/Atom" '
        'xmlns:opensearch="http://a9.com/-/spec/opensearch/1.1/">'
        f"<opensearch:totalResults>{total}</opensearch:totalResults>{items}</feed>"
    )


def forum_page(hits: list[dict[str, Any]], *, nb_pages: int = 1) -> tuple[int, Any]:
    """The ForumMagnum ``/api/search`` response shape.

    A TOP-LEVEL LIST of index results, not an object — ``_search_page`` takes ``data[0]`` and
    rejects anything that is not a non-empty list. Wrapping it in ``{"results": [...]}`` (the
    shape the *other* forum endpoints use) makes every page look like a transport failure, which
    is a very convincing way to write a robustness test that passes for the wrong reason."""
    return 200, [{"hits": hits, "nbPages": nb_pages}]


def af_page(posts: list[dict[str, Any]]) -> tuple[int, Any]:
    """The AF GraphQL response shape."""
    return 200, {"data": {"posts": {"results": posts}}}
