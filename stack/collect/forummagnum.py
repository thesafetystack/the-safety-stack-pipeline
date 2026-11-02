"""ForumMagnum GraphQL helpers shared by the forum collectors that page ``<base>/graphql``
(``collect.af_graphql``: every Alignment Forum post; ``collect.lw_graphql``: every LessWrong post
under the AI tag). Both sites run the same ForumMagnum backend and answer the same
``posts(input:{terms:{view:"new", …}})`` query shape.

What is here is the part of the two collectors that is the same: the strict-date guard for values
interpolated inline into query text, the GraphQL response parse, the descending offset-paging loop
with its ``complete | truncated | skip_cap | error`` receipt, the failed-page error mapping, and the
post normalisation (posting date, authors, ``fm_post`` keys).

**Skip limit** (D645). ForumMagnum refuses an ``offset`` past its maximum skip with an HTTP 200 whose
body is a GraphQL error envelope, ``"Exceeded maximum value for skip"`` (live 2026-10-19 on the
Alignment Forum: offset 2,000 answered with 200 posts, offset 2,200 refused, with or without
``htmlBody``). So one ``after``/``before`` listing reaches at most ~2,200 posts, however high
``max_records`` is. :func:`posts_results` tells that refusal apart (:data:`SKIP_REFUSED`) and
:func:`page_descending` ends the pull ``skip_cap``: a server cap, not a garbled page; a collector
splits the window or writes it ``partial`` (capped), never ``failed_permanent[parse_error]``. What is not here is per-collector on
purpose: the query text, the module-level test seams (``http_post``, ``_sleep``, ``PAGE_SIZE``,
which ``tests/test_collect_robustness.py`` monkeypatches on each workflow module) and the stage
itself. The paging loop therefore takes the page function and the page size as arguments, read
from the calling module at call time.

Imports only ``stack.*`` and the stdlib.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import Any

from stack.collect.base import utc
from stack.scholarly_records import keys_for_source


class _SkipRefused:
    """The type of :data:`SKIP_REFUSED`."""

    def __repr__(self) -> str:
        return "SKIP_REFUSED"


#: :func:`posts_results` for the server's skip-limit refusal (module docstring, "Skip limit").
SKIP_REFUSED = _SkipRefused()
#: The error message ForumMagnum answers an ``offset`` past its maximum skip with (live 2026-10-19).
SKIP_LIMIT_MESSAGE = "Exceeded maximum value for skip"

#: One page fetch: ``(offset, limit) -> (http_status, results | None | SKIP_REFUSED)``.
PageFn = Callable[[int, int], tuple[int, "list[dict[str, Any]] | _SkipRefused | None"]]

_QUERY_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_ISO_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")


def as_query_date(value: str, *, source: str) -> str:
    """Guard a date string before it is interpolated INLINE into GraphQL query text.

    The live-verified query shape uses inline terms (not GraphQL variables), so a date is the one
    field a config- or env-controlled string could break out of: a value carrying a quote would
    corrupt or inject into the query. Accept ONLY a strict, calendar-valid ``YYYY-MM-DD``; raise
    ``ValueError`` naming ``source`` otherwise."""
    if not _QUERY_DATE.match(value):
        raise ValueError(
            f"{source}: {value!r} is not a strict YYYY-MM-DD date (the ForumMagnum GraphQL `after` "
            f"term is interpolated inline; only a bare date literal is allowed)"
        )
    try:
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{source}: {value!r} is not a valid calendar date") from exc
    return value


def skip_refused(data: Any) -> bool:
    """Whether a response body is ForumMagnum's skip-limit refusal: a GraphQL ``errors`` entry whose
    message carries :data:`SKIP_LIMIT_MESSAGE`."""
    errors = data.get("errors") if isinstance(data, dict) else None
    return isinstance(errors, list) and any(
        isinstance(e, dict) and SKIP_LIMIT_MESSAGE in str(e.get("message") or "") for e in errors)


def posts_results(status: int, data: Any) -> list[dict[str, Any]] | _SkipRefused | None:
    """The ``data.posts.results`` list of a ``posts`` response; :data:`SKIP_REFUSED` for a 200
    carrying the skip-limit refusal (module docstring, D645); ``None`` for a non-200, any other
    GraphQL error envelope (``data`` null or missing) or any other shape, so the caller folds them
    into one ``error`` outcome."""
    if status != 200 or not isinstance(data, dict):
        return None
    if skip_refused(data):
        return SKIP_REFUSED
    payload = data.get("data")
    if not isinstance(payload, dict):
        return None
    posts = payload.get("posts")
    if not isinstance(posts, dict):
        return None
    results = posts.get("results")
    return results if isinstance(results, list) else None


def page_descending(
    fetch_page: PageFn,
    limit: int,
    page_size: int,
    *,
    on_page: Callable[[], None] | None = None,
    stats: dict[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], str]:
    """Page a ``view:"new"`` (newest-first) query by offset up to ``limit`` posts. Returns
    ``(posts, exhausted)`` with ``exhausted`` in {complete, truncated, skip_cap, error}:
    ``complete`` = the API ran out of results before the cap (an empty or short page);
    ``truncated`` = the cap stopped a DESCENDING pull, so an unknown newest prefix was covered and
    the caller claims nothing (SHOULD-14); ``skip_cap`` = the server refused the next offset (its
    skip limit, D645; ``stats['skip_offset']`` = that offset): the listing holds more posts than
    offset paging reaches, the posts so far are an unknown newest prefix as for ``truncated``;
    ``error`` = a page failed or was garbled mid-pull (``stats['status']`` = its HTTP status,
    ``0`` = transport failure). ``on_page`` is called once per fetched page."""
    posts: list[dict[str, Any]] = []
    offset = 0
    while True:
        remaining = limit - len(posts)
        if remaining <= 0:
            return posts, "truncated"
        size = min(page_size, remaining)
        status, results = fetch_page(offset, size)
        if on_page is not None:
            on_page()
        if isinstance(results, _SkipRefused):
            if stats is not None:
                stats["skip_offset"] = offset
            return posts, "skip_cap"
        if status != 200 or results is None:
            if stats is not None:
                stats["status"] = status
            return posts, "error"
        if not results:
            return posts, "complete"
        posts.extend(results)
        offset += len(results)
        if len(results) < size:
            return posts, "complete"


def error_class_of(status: int) -> tuple[str, bool]:
    """A failed page's HTTP status → ``(error_class, permanent)`` for its coverage window: ``0``
    transport → ``network``; 429 → ``rate_limit``; 5xx → ``http_5xx``; 401/403 →
    ``provider_auth``; a 200 whose body is not the expected shape (a GraphQL error envelope) →
    ``parse_error`` (permanent); anything else → ``network``."""
    if status == 429:
        return "rate_limit", False
    if 500 <= status < 600:
        return "http_5xx", False
    if status in (401, 403):
        return "provider_auth", False
    if status == 200:
        return "parse_error", True
    return "network", False


def posted_date(post: dict[str, Any]) -> str | None:
    """The post's ISO ``YYYY-MM-DD`` posting date from its ``postedAt`` timestamp, else ``None``."""
    val = post.get("postedAt")
    if isinstance(val, str):
        m = _ISO_DATE.match(val.strip())
        if m:
            return m.group(1)
    return None


def posted_at(post: dict[str, Any]) -> dt.datetime | None:
    """``postedAt`` as an aware UTC datetime (``metadata.published_at``), ``None`` if absent or
    malformed (a bad provider timestamp never fails the run)."""
    val = post.get("postedAt")
    if not isinstance(val, str):
        return None
    try:
        return utc(val)
    except ValueError:
        return None


def post_authors(post: dict[str, Any]) -> list[str]:
    """Author display names: the primary ``user`` first, then each ``coauthor`` (displayName,
    else username; blanks dropped)."""
    names: list[str] = []
    people: list[Any] = [post.get("user")]
    coauthors = post.get("coauthors")
    if isinstance(coauthors, list):
        people.extend(coauthors)
    for p in people:
        if isinstance(p, dict):
            nm = p.get("displayName") or p.get("username")
            if isinstance(nm, str) and nm.strip():
                names.append(nm.strip())
    return names


def fm_keys(channel_group: str, channel: str, post_id: str) -> list[tuple[str, str]]:
    """The row's identity keys (``fm_post`` = the post ``_id``) as the harness's pairs."""
    return [(k["kind"], k["value"]) for k in keys_for_source(channel_group, channel, post_id)]

