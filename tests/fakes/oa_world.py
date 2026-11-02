"""A fake scholarly web for the OA lane's request-efficiency replay (T3.x-f4; network never touched).

:class:`OaWorld` satisfies :class:`stack.http.HttpGetter` and answers by URL pattern, so the real
``fetchpdf`` chain (resolver, tiers, legacy PDF chain) can run end to end against it under the
lane's :class:`stack.oa_adapter.RequestGate`. Every request is logged (:attr:`OaWorld.calls`);
unknown URLs answer 404 (a publisher site that has nothing), not an error, because fetchpdf
probes many hosts speculatively and the replay measures exactly that.

Papers are registered per DOI with a class:

* ``hit``: Unpaywall and OpenAlex name an OA PDF on the publisher host, the PDF is served;
* ``miss``: Crossref knows the DOI, Unpaywall says ``is_oa: false``, the landing page has no PDF;
* ``unlisted``: as ``miss`` but Unpaywall does not know the DOI (404; e.g. a DOI registered days ago);
* ``zenodo``: a DataCite DOI ``10.5281/zenodo.<id>`` (Crossref and Unpaywall 404); the Zenodo
  record API lists one open PDF; the landing page names it in ``citation_pdf_url``;
* ``mdpi``: an MDPI DOI ``10.3390/…``; Crossref and Unpaywall answer as for an OA article whose
  ``url_for_pdf`` is the MDPI ``/pdf`` URL; the landing page names it in ``citation_pdf_url``.

``blocked_hosts`` answer 403 to every request (T3.6 saw no Zenodo or MDPI copy through fetchpdf;
``fetchpdf`` itself notes MDPI serves 403 to plain requests). :func:`b1_mix` is the broader run's
OA retrieval mix (``rcprvcnepyb1g``: 80 retrievals over 88 members, 8 without a DOI).
"""

from __future__ import annotations

import json
import re
import urllib.parse
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from stack.http import HttpResponse

CLASSES = ("hit", "miss", "unlisted", "zenodo", "mdpi")


def pdf_bytes(text: str) -> bytes:
    """A small valid one-page PDF whose text layer carries ``text`` (pypdf reads it)."""
    lines = [text] + ["We study the problem stated in the title and report results on it."] * 12
    ops = " ".join(f"({ln.replace('(', '').replace(')', '')}) Tj 0 -16 Td" for ln in lines)
    stream = f"BT /F1 11 Tf 72 740 Td {ops} ET".encode()  # over fetchpdf's 200-char text floor
    pad = b"%" + b"x" * 2000 + b"\n"  # over fetchpdf's 1 KB "implausibly small" floor
    objs = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
         b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = bytearray(b"%PDF-1.4\n" + pad), []
    for n, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % n + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)


@dataclass
class Paper:
    doi: str
    cls: str
    title: str

    @property
    def zenodo_id(self) -> str:
        m = re.search(r"zenodo\.(\d+)", self.doi)
        return m.group(1) if m else ""

    @property
    def landing(self) -> str:
        suffix = self.doi.split("/", 1)[1]
        if self.cls == "zenodo":
            return f"https://zenodo.org/records/{self.zenodo_id}"
        if self.cls == "mdpi":
            return f"https://www.mdpi.com/2071-1050/{suffix.replace('.', '/')}"
        return f"https://pub-{self.doi.split('/')[0].replace('.', '-')}.test/article/{suffix}"

    @property
    def pdf_url(self) -> str:
        if self.cls == "zenodo":
            return f"https://zenodo.org/records/{self.zenodo_id}/files/paper.pdf"
        return f"{self.landing}/pdf" if self.cls in ("mdpi", "hit") else ""

    @property
    def pdf(self) -> bytes:
        return pdf_bytes(f"{self.title} doi:{self.doi}")


@dataclass
class OaWorld:
    """URL pattern → answer over registered papers (module docstring)."""

    papers: dict[str, Paper] = field(default_factory=dict)
    blocked_hosts: frozenset[str] = frozenset()
    calls: list[str] = field(default_factory=list)

    def add(self, doi: str, cls: str, title: str = "") -> Paper:
        assert cls in CLASSES, cls
        p = Paper(doi.lower(), cls, title or f"A study of {doi}")
        self.papers[p.doi] = p
        return p

    # ── the HttpGetter ────────────────────────────────────────────────────
    def get(self, url: str, *, headers: Mapping[str, str], timeout: float, max_bytes: int) -> HttpResponse:
        self.calls.append(url)
        parts = urllib.parse.urlsplit(url)
        host, path = (parts.hostname or "").lower(), urllib.parse.unquote(parts.path)
        if host in self.blocked_hosts:
            return _r(403, b"forbidden", url, "text/html")
        return self._answer(host, path, parts.query, url)

    __call__ = get

    def _paper(self, text: str) -> Paper | None:
        low = text.lower()
        for doi in sorted(self.papers, key=len, reverse=True):
            if doi in low:
                return self.papers[doi]
        return None

    def _answer(self, host: str, path: str, query: str, url: str) -> HttpResponse:
        p = self._paper(path) or self._paper(urllib.parse.unquote(query))
        if "idconv" in path:
            ids = urllib.parse.parse_qs(query).get("ids", [""])[0].split(",")
            return _json(url, {"status": "ok", "records": [{"requested-id": i, "errmsg": "invalid article id"}
                                                           for i in ids if i]})
        if host == "api.crossref.org":
            if p is None:
                return _json(url, {"status": "ok", "message": {"items": [], "total-results": 0}})
            if p.cls == "zenodo":
                return _r(404, b"Resource not found.", url, "text/plain")
            return _json(url, {"status": "ok", "message": {
                "DOI": p.doi, "title": [p.title], "type": "journal-article", "link": [],
                "license": [{"URL": "https://creativecommons.org/licenses/by/4.0/"}]
                if p.cls not in ("miss", "unlisted") else []}})
        if host == "api.unpaywall.org":
            if p is None or p.cls in ("zenodo", "unlisted"):
                return _json(url, {"HTTP_status_code": 404, "error": True}, status=404)
            loc = ({"url": p.landing, "url_for_pdf": p.pdf_url, "url_for_landing_page": p.landing,
                    "host_type": "publisher", "version": "publishedVersion", "license": "cc-by"}
                   if p.cls in ("hit", "mdpi") else None)
            return _json(url, {"doi": p.doi, "title": p.title, "is_oa": loc is not None,
                               "best_oa_location": loc, "oa_locations": [loc] if loc else []})
        if host == "api.openalex.org":
            if p is None:
                return _json(url, {"results": [], "meta": {"count": 0}})
            oa = p.cls not in ("miss", "unlisted")
            loc = {"pdf_url": p.pdf_url or None, "landing_page_url": p.landing, "is_oa": oa}
            return _json(url, {"id": "https://openalex.org/W1", "doi": f"https://doi.org/{p.doi}", "title": p.title,
                               "ids": {"doi": f"https://doi.org/{p.doi}"},
                               "open_access": {"is_oa": oa, "oa_url": p.pdf_url or None},
                               "best_oa_location": loc if oa else None, "locations": [loc]})
        if host == "api.semanticscholar.org":
            if p is None:
                return _json(url, {"error": "not found"}, status=404)
            return _json(url, {"paperId": "s2x", "externalIds": {"DOI": p.doi}, "title": p.title,
                               "openAccessPdf": None})
        if host == "api.datacite.org":
            if p is None or p.cls != "zenodo":
                return _json(url, {"errors": [{"status": "404"}]}, status=404)
            return _json(url, {"data": {"id": p.doi, "attributes": {
                "doi": p.doi, "url": p.landing, "titles": [{"title": p.title}], "relatedIdentifiers": [],
                "contentUrl": None, "types": {"resourceTypeGeneral": "Preprint"}}}})
        if host in ("doi.org", "dx.doi.org"):
            if p is None:
                return _r(404, b"DOI not found", url, "text/html")
            return HttpResponse(302, {"location": p.landing, "content-type": "text/html"}, b"", url)
        if host == "zenodo.org" and p is None:
            m = re.match(r"^/api/records/(\d+)$", path)
            z = next((q for q in self.papers.values() if q.cls == "zenodo" and q.zenodo_id
                      and (q.zenodo_id in path)), None)
            if m and z is not None:
                return _json(url, {"id": int(z.zenodo_id), "doi": z.doi, "metadata": {
                    "title": z.title, "access_right": "open", "resource_type": {"type": "publication"}},
                    "access": {"record": "public", "files": "public"},
                    "files": [{"key": "paper.pdf", "size": len(z.pdf),
                               "links": {"self": f"https://zenodo.org/api/records/{z.zenodo_id}/files/paper.pdf/content"}}]})
            if z is not None and path.endswith(("/files/paper.pdf", "/files/paper.pdf/content")):
                return _r(200, z.pdf, url, "application/pdf")
            if z is not None and path == f"/records/{z.zenodo_id}":
                return _r(200, _landing(z), url, "text/html; charset=utf-8")
            return _r(404, b"not found", url, "text/html")
        for q in self.papers.values():  # landing pages and PDFs on publisher hosts
            if url.split("?")[0] == q.landing:
                return _r(200, _landing(q), url, "text/html; charset=utf-8")
            if q.pdf_url and url.split("?")[0] == q.pdf_url:
                return _r(200, q.pdf, url, "application/pdf")
        return _r(404, b"<html><body>Not found</body></html>", url, "text/html")


def _landing(p: Paper) -> bytes:
    meta = f'<meta name="citation_pdf_url" content="{p.pdf_url}">' if p.pdf_url else ""
    body = (f"<html><head><title>{p.title}</title><meta name=\"citation_title\" content=\"{p.title}\">"
            f"<meta name=\"citation_doi\" content=\"{p.doi}\">{meta}</head><body><h1>{p.title}</h1>"
            f"<p>Abstract. {'Lorem ipsum dolor sit amet. ' * 20}</p></body></html>")
    return body.encode()


def _r(status: int, body: bytes, url: str, ctype: str) -> HttpResponse:
    return HttpResponse(status, {"content-type": ctype}, body, url)


def _json(url: str, data: object, status: int = 200) -> HttpResponse:
    return _r(status, json.dumps(data).encode(), url, "application/json")


#: The broader run's OA retrieval mix (``rcprvcnepyb1g``, T3.6): per class the number of members,
#: all channel ``openalex`` (``full_text`` + ``xml``) except one ``semantic_scholar`` hit.
B1_MIX: dict[str, int] = {"zenodo": 31, "mdpi": 4, "miss": 27, "hit": 18}


def b1_mix(world: OaWorld, mix: Mapping[str, int] = B1_MIX) -> list[tuple[Paper, str]]:
    """Register the broader run's mix (or ``mix``) in ``world``: ``(paper, channel)`` per retrieval."""
    out: list[tuple[Paper, str]] = []
    for cls, n in mix.items():
        for i in range(n):
            doi = {"zenodo": f"10.5281/zenodo.{23000000 + i}", "mdpi": f"10.3390/su18{i:06d}",
                   "miss": f"10.{1000 + i % 9}/miss.{i}", "hit": f"10.{2000 + i % 7}/hit.{i}",
                   "unlisted": f"10.{3000 + i % 5}/unlisted.{i}"}[cls]
            channel = "semantic_scholar" if (cls == "hit" and i == 0) else "openalex"
            out.append((world.add(doi, cls), channel))
    return out


def hosts(calls: Iterable[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for u in calls:
        h = urllib.parse.urlsplit(u).hostname or ""
        out[h] = out.get(h, 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


__all__ = ["B1_MIX", "CLASSES", "OaWorld", "Paper", "b1_mix", "hosts", "pdf_bytes"]
