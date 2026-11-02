"""Fake acquire lanes (T3.1): no network. A :class:`FakeLane` answers each
``(source_id, content_role)`` from a script of canned answers, minting raw snapshots from canned
bytes into the run's blob store, and records every request it saw.

Answers: ``b"…"`` (bytes: ``ok``, one raw snapshot of those bytes), ``"ok"`` (the same body every
time: equal digest after the first), ``"transient"`` (``failed_transient[http_5xx]``), ``"dead"``
(``failed_permanent[dead]``), ``"429"`` (a provider halt: ``halt_run``), ``"rate_limit"`` (a
host's or provider's limit on this role only: ``failed_transient[rate_limit]``, no halt, a
``Retry-After`` hint of :data:`RATE_LIMIT_HINT` after the request's ``now``), ``"crash"`` (the lane
raises). A key's script is consumed one answer per call; the last answer repeats.

:class:`TrapLane` and :class:`TrapTransport` fail the test when touched (zero-network runs such as
``--only convert``): ``pytest.fail`` raises a ``BaseException``, so the skeleton's per-lane
``except Exception`` cannot swallow it as a lane crash.

:class:`FakeFetchpdf` stands in for ``fetchpdf`` behind the OA lane (T3.4): its :meth:`loader` is
what ``OaLane(load=…)`` takes, returning a chain module that can (or cannot) prove the
open-access-only posture and a session whose ``retrieve`` answers from a table of artifacts. With
``over_http=True`` it behaves like the real engine on the wire: ``retrieve`` first GETs the
identifier's ``lookups`` URLs (resolver APIs) and then downloads each artifact URL with
``requests`` (alternating a module-level ``requests.get`` and a long-lived ``requests.Session``,
as fetchpdf's T5 chain and its ``HttpClient`` do), keeping an artifact only on a 200; the bytes
come from whatever transport answers (the OA lane's request gate routes them to the test's
``FakeHttp``; outside the gate they would hit the network, which the test guard blocks).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

from workflows.acquire.lanes import FetchOutcome, FetchRequest, LaneEnv

Answer = bytes | str
#: The ``Retry-After`` the ``"rate_limit"`` answer gives (module docstring).
RATE_LIMIT_HINT = dt.timedelta(hours=1)


@dataclass
class FakeLane:
    name: str
    script: dict[tuple[str, str], list[Answer]] = field(default_factory=dict)
    default: Answer = "transient"
    media_type: str = "application/pdf"
    calls: list[FetchRequest] = field(default_factory=list)

    @classmethod
    def answering(cls, name: str, script: Mapping[tuple[str, str], Answer | Sequence[Answer]],
                  default: Answer = "transient", **kw: Any) -> FakeLane:
        norm = {k: [v] if isinstance(v, (bytes, str)) else list(v) for k, v in script.items()}
        return cls(name, norm, default, **kw)

    def _next(self, key: tuple[str, str]) -> Answer:
        seq = self.script.get(key)
        if not seq:
            return self.default
        return seq.pop(0) if len(seq) > 1 else seq[0]

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        self.calls.append(request)
        key = (request.source_id, request.content_role)
        ans = self._next(key)
        url = f"https://fake.test/{request.lane}/{request.source_id}/{request.content_role}"
        if ans == "ok":
            ans = f"body of {request.source_id}/{request.content_role}".encode()
        if isinstance(ans, bytes):
            digest = env.blobs.put(ans)
            snap = {"digest": digest, "bytes": len(ans), "media_type": self.media_type, "url": url,
                    "final_url": url, "http_status": 200, "via": "live"}
            return FetchOutcome("ok", snapshots=[snap], role_patch={"tried_urls": [url], "etag": digest[:8]})
        if ans == "transient":
            return FetchOutcome("failed_transient", "http_5xx", f"{url}: 503",
                                role_patch={"tried_urls": [url]})
        if ans == "dead":
            return FetchOutcome("failed_permanent", "dead", f"{url}: 404", role_patch={"tried_urls": [url]})
        if ans == "429":
            return FetchOutcome("failed_transient", "rate_limit", f"{url}: 429", halt_run=True)
        if ans == "rate_limit":
            return FetchOutcome("failed_transient", "rate_limit", f"{url}: 429", role_patch={"tried_urls": [url]},
                                retry_after_hint=request.now + RATE_LIMIT_HINT)
        if ans == "crash":
            raise RuntimeError(f"fake lane crash on {key}")
        raise ValueError(f"FakeLane: unknown answer {ans!r}")

    def keys(self) -> list[tuple[str, str]]:
        """``(source_id, content_role)`` of every call, in order."""
        return [(c.source_id, c.content_role) for c in self.calls]


@dataclass
class TrapLane:
    """A lane that fails the test if ``fetch`` is called."""

    name: str

    def fetch(self, request: FetchRequest, env: LaneEnv) -> FetchOutcome:
        pytest.fail(f"lane {self.name!r} called in a zero-network run ({request.doc_id}/{request.content_role})")


class TrapTransport:
    """An HTTP transport that fails the test on any use."""

    def __getattr__(self, name: str) -> Any:
        pytest.fail(f"HTTP transport used in a zero-network run (.{name})")

    def __call__(self, *a: Any, **kw: Any) -> Any:
        pytest.fail("HTTP transport called in a zero-network run")


@dataclass
class FakeFetchpdf:
    """``fetchpdf`` for the OA lane: ``artifacts[identifier]`` = ``[(bytes, url, source), …]``
    (structured first, as fetchpdf orders them); an unknown identifier yields no artifact.
    ``build``: ``'open'`` (reports nothing reachable), ``'stuck'`` (a last-resort source stays
    enabled after being asked to turn it off). Every ``retrieve`` call is recorded."""

    artifacts: dict[str, list[tuple[bytes, str, str]]] = field(default_factory=dict)
    build: str = "open"
    calls: list[dict[str, Any]] = field(default_factory=list)
    loads: int = 0
    over_http: bool = False
    lookups: dict[str, list[str]] = field(default_factory=dict)

    def chain(self) -> Any:
        if self.build == "open":
            return SimpleNamespace(last_resorts_enabled=lambda: False)
        if self.build == "stuck":
            return SimpleNamespace(disable_last_resorts=lambda: None, last_resorts_enabled=lambda: True)
        raise ValueError(f"FakeFetchpdf: unknown build {self.build!r}")

    def loader(self, scratch: str, email: str) -> tuple[Any, Any]:
        from stack.oa_adapter import FetchpdfSession

        self.loads += 1
        assert email, "the OA lane must hand fetchpdf a contact address"
        return self.chain(), FetchpdfSession(retrieve=self.retrieve, kwargs={"email": email})

    def retrieve(self, raw_identifier: str, **kw: Any) -> Any:
        self.calls.append({"raw": raw_identifier, **kw})
        if self.over_http:
            return self._over_http(raw_identifier)
        arts = [SimpleNamespace(content=body, url=url, source=source, http_status=200)
                for body, url, source in self.artifacts.get(raw_identifier, [])]
        return SimpleNamespace(artifacts=arts, reason="" if arts else "no open-access copy found")

    def _over_http(self, raw_identifier: str) -> Any:
        import requests

        session = requests.Session()
        for url in self.lookups.get(raw_identifier, []):
            session.get(url, timeout=5)
        arts = []
        for n, (_body, url, source) in enumerate(self.artifacts.get(raw_identifier, [])):
            try:
                r = requests.get(url, timeout=5) if n % 2 else session.get(url, timeout=5)
            except requests.exceptions.InvalidURL:  # as fetchpdf's client: that URL is given up
                continue
            if r.status_code == 200:
                arts.append(SimpleNamespace(content=r.content, url=url, source=source, http_status=200))
        return SimpleNamespace(artifacts=arts, reason="" if arts else "no open-access copy found")


__all__ = ["FakeFetchpdf", "FakeLane", "TrapLane", "TrapTransport"]
