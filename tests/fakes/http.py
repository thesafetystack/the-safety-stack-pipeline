"""A fake at the collector transport seam (:class:`stack.collect.base.Sender`; no sockets).

Routes map ``(method, full URL)`` (params folded in by :func:`stack.collect.base.full_url`, so
register the same ``params`` the collector sends) to a canned response. Faults are injected
per route as a queue that is served before the canned answer (``429`` with a ``Retry-After``,
``5xx``, a transport failure). Every call is logged. An unrouted call raises ``LookupError``:
a fixture gap is loud, never a silent network read.

    fake = FakeHttp()
    fake.add("https://api.test/works", {"page": 1}, json={"results": [...]})
    fake.inject("https://api.test/works", {"page": 2}, status=429, retry_after=3600)
    t = collector.transport(PACING, send=fake, sleep=lambda s: None)

It also satisfies :class:`stack.http.HttpGetter` through :meth:`FakeHttp.get`.
"""

from __future__ import annotations

import json as _json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stack.collect.base import full_url
from stack.http import HttpResponse


@dataclass
class Call:
    method: str
    url: str
    body: bytes | None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Canned:
    status: int | None
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    error: str | None = None

    def response(self, url: str) -> HttpResponse:
        return HttpResponse(self.status, {k.lower(): v for k, v in self.headers.items()}, self.body,
                            url, error=self.error)


def _body(json: Any, body: bytes | str | None) -> bytes:
    if json is not None:
        return _json.dumps(json).encode()
    if isinstance(body, str):
        return body.encode()
    return body or b""


class FakeHttp:
    """URL/params → canned response, fault injection, call log."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Canned] = {}
        self.faults: dict[tuple[str, str], list[Canned]] = {}
        self.calls: list[Call] = []

    @staticmethod
    def key(method: str, url: str, params: Mapping[str, Any] | None = None) -> tuple[str, str]:
        return method.upper(), full_url(url, params)

    def add(self, url: str, params: Mapping[str, Any] | None = None, *, json: Any = None,
            body: bytes | str | None = None, status: int = 200,
            headers: Mapping[str, str] | None = None, method: str = "GET") -> None:
        """The canned answer for ``(method, url?params)`` (served on every call after faults)."""
        self.routes[self.key(method, url, params)] = Canned(status, _body(json, body),
                                                             dict(headers or {}))

    def inject(self, url: str, params: Mapping[str, Any] | None = None, *, status: int | None = 503,
               times: int = 1, retry_after: float | None = None, json: Any = None,
               body: bytes | str | None = None, error: str | None = None,
               method: str = "GET") -> None:
        """Queue ``times`` faults before the route's canned answer: an HTTP ``status`` (429 with
        ``retry_after`` sets the ``Retry-After`` header) or, with ``status=None``, a transport
        failure named ``error`` (default ``timeout``)."""
        headers = {"Retry-After": str(retry_after)} if retry_after is not None else {}
        fault = Canned(status, _body(json, body), headers,
                       error=(error or "timeout") if status is None else None)
        self.faults.setdefault(self.key(method, url, params), []).extend([fault] * times)

    def __call__(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                 timeout: float, max_bytes: int) -> HttpResponse:
        self.calls.append(Call(method.upper(), url, body, dict(headers)))
        key = (method.upper(), url)
        queue = self.faults.get(key)
        if queue:
            return queue.pop(0).response(url)
        if key not in self.routes:
            raise LookupError(f"FakeHttp: no route for {method} {url}")
        return self.routes[key].response(url)

    def get(self, url: str, *, headers: Mapping[str, str], timeout: float,
            max_bytes: int) -> HttpResponse:
        """:class:`stack.http.HttpGetter` shape."""
        return self(method="GET", url=url, headers=headers, body=None, timeout=timeout,
                    max_bytes=max_bytes)

    def urls(self) -> list[str]:
        return [c.url for c in self.calls]


#: Fakes by name, for a stage module the runner loads by path (it cannot be handed an object):
#: the test installs ``INSTALLED["fake"] = FakeHttp()``, the toy stage reads it.
INSTALLED: dict[str, FakeHttp] = {}


__all__ = ["INSTALLED", "Call", "Canned", "FakeHttp"]
