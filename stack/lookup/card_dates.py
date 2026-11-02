"""Deterministic publication-date evidence for official card documents and listings."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urldefrag, urljoin

VERSION = 1
MONTH = r'(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)'
DATE_RE = re.compile(rf'(?<!\d)(?:\d{{4}}-\d{{2}}-\d{{2}}|{MONTH}\.?\s+\d{{1,2}},?\s+\d{{4}}|\d{{1,2}}\s+{MONTH}\.?\s+\d{{4}}|{MONTH}\.?\s+\d{{4}}|\d{{4}}-\d{{2}}|(?:19|20)\d{{2}})(?!\d)', re.IGNORECASE)


def evidence(raw: str, method: str, kind: str = 'publication') -> dict[str, Any] | None:
    match = DATE_RE.search(raw)
    if not match:
        return None
    value = re.sub(r'\bSept\b', 'Sep', match.group(), flags=re.IGNORECASE).replace('.', '')
    parsed = None
    precision = 'unknown'
    for fmt, p in [('%Y-%m-%d', 'day'), ('%B %d, %Y', 'day'), ('%b %d, %Y', 'day'),
                   ('%B %d %Y', 'day'), ('%b %d %Y', 'day'), ('%d %B %Y', 'day'),
                   ('%d %b %Y', 'day'), ('%B %Y', 'month'), ('%b %Y', 'month'),
                   ('%Y-%m', 'month'), ('%Y', 'year')]:
        try:
            dt = datetime.strptime(value, fmt)
            precision = p
            parsed = dt.strftime('%Y-%m-%d' if p == 'day' else '%Y-%m' if p == 'month' else '%Y')
            break
        except ValueError:
            pass
    if parsed is None:
        return None
    return {'value': parsed, 'precision': precision, 'kind': kind, 'method': method, 'raw': raw[:1000]}


def resolve(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    unique = list({json.dumps(c, sort_keys=True): c for c in candidates}.values())
    pubs = [c for c in unique if c['kind'] == 'publication']
    days = {c['value'] for c in pubs if c['precision'] == 'day'}
    selected = next(iter(days)) if len(days) == 1 else None
    conflict = any(not (a['value'].startswith(b['value']) or b['value'].startswith(a['value'])) for a in pubs for b in pubs)
    return {'parser_version': VERSION, 'state': 'conflict' if conflict else 'found' if selected else 'partial' if pubs else 'missing',
            'pub_date': None if conflict else selected, 'candidates': unique}


@dataclass
class Node:
    tag: str
    attrs: dict[str, str]
    parent: Node | None = None
    children: list[Any] = field(default_factory=list)

    def text(self) -> str:
        return ''.join(c.text() if isinstance(c, Node) else c for c in self.children)

    def walk(self):
        yield self
        for c in self.children:
            if isinstance(c, Node):
                yield from c.walk()


class Tree(HTMLParser):
    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.root = Node('root', {})
        self.current = self.root
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or '' for k, v in attrs}, self.current)
        self.current.children.append(node)
        if tag not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}:
            self.current = node

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                return
            node = node.parent

    def handle_data(self, data):
        self.current.children.append(data)


def structured(tree: Tree, url: str, *, listing=False) -> list[dict[str, Any]]:
    out = []
    def add(raw, method, kind='publication'):
        if isinstance(raw, str) and (e := evidence(raw, method, kind)):
            out.append(e)
    def visit(obj):
        if isinstance(obj, list):
            for v in obj: visit(v)
        elif isinstance(obj, dict):
            identifiers = [obj.get(k) for k in ('url', '@id', 'mainEntityOfPage')]
            identifiers = [v.get('@id', v.get('url')) if isinstance(v, dict) else v for v in identifiers]
            matched = any(isinstance(v, str) and urldefrag(urljoin(url, v))[0].rstrip('/') == url.rstrip('/') for v in identifiers)
            # Only the current document, never a related article or embedded item.
            if matched or not listing and not any(identifiers) and obj.get('@type') in ('Article','TechArticle','Report','ScholarlyArticle','WebPage'):
                add(obj.get('datePublished'), 'jsonld.datePublished')
                add(obj.get('dateModified'), 'jsonld.dateModified', 'modification')
            for v in obj.values():
                if isinstance(v, (dict, list)): visit(v)
    for n in tree.root.walk():
        if n.tag == 'script' and n.attrs.get('type') == 'application/ld+json':
            try: visit(json.loads(n.text()))
            except (ValueError, TypeError): pass
        if not listing and n.tag == 'meta':
            key = (n.attrs.get('property') or n.attrs.get('name') or n.attrs.get('itemprop') or '').lower()
            if key in {'article:published_time','datepublished','citation_publication_date','citation_date','dc.date.issued'}:
                add(n.attrs.get('content'), 'meta.' + key)
            elif key in {'article:modified_time', 'datemodified'}:
                add(n.attrs.get('content'), 'meta.' + key, 'modification')
    return out


def text_dates(text: str) -> list[dict[str, Any]]:
    out = []
    # Publication labels in citations or revision histories are not document metadata.
    lines = re.split(r'(?im)^\s*#{0,3}\s*(?:references|bibliography|revision history|version history)\s*$', text[:6000])[0].splitlines()[:60]
    card_header = bool(re.search(r'(?:system|model)\s+card|responsible scaling policy|safety framework', text[:1600], re.IGNORECASE))
    for i, line in enumerate(lines):
        line = line.strip().strip('#*| ')
        kind = None
        if re.search(r'last\s+(?:updated|modified)|revision\s+date|updated\s*:', line, re.IGNORECASE): kind = 'modification'
        elif re.search(r'model\s+(?:release|launch)\s+date|release\s+date|effective\s+(?:date|from)', line, re.IGNORECASE): kind = 'model_release'
        elif re.match(r'(?:first\s+)?(?:published|publication date|date published|date of publication|issued)\b', line, re.IGNORECASE) or i < 15 and card_header and DATE_RE.fullmatch(line): kind = 'publication'
        if kind:
            # A label may have its value on the next line in converted PDFs.
            raw = line if DATE_RE.search(line) else line + ' ' + (lines[i+1].strip() if i+1 < len(lines) else '')
            if e := evidence(raw, 'document_label' if not DATE_RE.fullmatch(line) else 'document_dateline', kind): out.append(e)
    return out


def document_dates(content: str, url: str, html: bool = False) -> list[dict[str, Any]]:
    if not html:
        return text_dates(content)
    tree = Tree(content)
    out = structured(tree, url)
    for n in tree.root.walk():
        if n.tag == 'time' and (n.attrs.get('itemprop') == 'datePublished' or 'publish' in (n.attrs.get('class','') + n.attrs.get('aria-label','')).lower()) \
                and (e := evidence(n.attrs.get('datetime') or n.text(), 'time.datePublished')):
            out.append(e)
    # Preserve DOM block boundaries; do not parse script/navigation dates as prose.
    blocks = [n.text() for n in tree.root.walk() if n.tag in {'h1','h2','p','time'} and not any(isinstance(c,Node) and c.tag in {'p','time'} for c in n.children)]
    out.extend(text_dates('\n'.join(blocks)))
    return out


def listing_dates(html: str, base_url: str) -> dict[str, list[dict[str, Any]]]:
    tree = Tree(html)
    result = {}
    for anchor in tree.root.walk():
        if anchor.tag != 'a' or not anchor.attrs.get('href'):
            continue
        url = urljoin(base_url, anchor.attrs['href'])
        out = structured(tree, url, listing=True)
        # Only this anchor or its single-link entry; never a multi-card container.
        entry = anchor
        for _ in range(3):
            parent = entry.parent
            if not parent or parent.tag in {'body','root','html'}: break
            links = {urljoin(base_url,n.attrs['href']) for n in parent.walk() if n.tag=='a' and n.attrs.get('href')}
            if links != {url}: break
            entry = parent
        label = anchor.text().strip()
        if DATE_RE.match(label) and (e := evidence(label, 'listing_dateline')):
            out.append(e)
        for n in entry.walk():
            if n.tag == 'time':
                label = n.attrs.get('itemprop','') + ' ' + n.attrs.get('class','') + ' ' + entry.text()[:120]
                if re.search('updated|modified', label, re.IGNORECASE): kind='modification'
                elif re.search('publish', label, re.IGNORECASE): kind='publication'
                else: kind='publication'
                if e := evidence(n.attrs.get('datetime') or n.text(), 'listing_time', kind): out.append(e)
        if not re.search(r'updated|modified|release date|effective', entry.text(), re.IGNORECASE):
            for n in entry.walk():
                if n.tag in {'span', 'p'} and DATE_RE.fullmatch(n.text().strip()) \
                        and (e := evidence(n.text().strip(), 'listing_entry_date')):
                    out.append(e)
        out.extend(text_dates(entry.text()))
        result.setdefault(url, []).extend(out)
    return result
