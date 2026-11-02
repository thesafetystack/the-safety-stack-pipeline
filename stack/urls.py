"""``stack.urls`` — the one home of URL rules (track U, round 4; ``docs/design/url-normalisation.md``).

Pure, stdlib only, no network. Four entry points, each a different question about one link:

* :func:`fetch_url` ``(raw, base=None) -> str | None`` — **what a lane fetches**: ``raw`` with
  literal and encoded whitespace (``%20``, ``%0A``, …) stripped from both ends, resolved against
  ``base``, http(s) only, redirect wrappers unwrapped (:data:`REDIRECT_WRAPPERS`,
  ``web.archive.org/web/<ts>/``, archive.today's ``/o/<id>/<url>``), scheme and host lower case,
  default port and credentials dropped, fragment dropped, tracking parameters dropped
  (:data:`TRACKING_PARAMS`, :data:`TRACKING_PREFIXES`, :data:`HOST_TRACKING_PARAMS`, Substack's on
  ``/p/`` paths); every other parameter and the path (trailing slash included) kept as given. A
  Google Doc (``docs.google.com/document/d/<id>/…``) is fetched as its plain-text export.
* :func:`url_key` ``(raw) -> str`` — **the merging key** of the ``url`` kind (``''`` when ``raw`` is
  not an absolute http(s) URL): the fetch form, then ``https``, host without ``www.`` (and
  ``m.``/``mobile.`` on :data:`MOBILE_HOSTS`, mirror hosts in :data:`HOST_ALIASES` folded), no
  trailing slash, the query reduced to the host's identity parameters (:data:`KEY_PARAMS`, plus a
  numeric WordPress ``p``/``page_id`` on any host) sorted, and the domain canonical forms of
  :func:`_canonical` (arXiv, doi.org, X, YouTube, OpenReview, Substack, Medium, GitHub,
  ``raw.githubusercontent.com``, forum posts, Google Docs). Path case is kept except GitHub's owner/repo.
* :func:`link_keys` ``(raw, base=None) -> LinkTarget | None`` — **what a link points at**: the
  identity group, the typed keys (through :mod:`stack.scholarly_records`, so the arXiv–DOI bridge
  applies), the fetch URL, a kind hint (:data:`KINDS`) and attributes (``arxiv_version``). arXiv,
  DOI (resolver and DOI-in-path publishers, :data:`DOI_PATH_PATTERNS`), PubMed → ``paper``;
  LessWrong / Alignment Forum / GreaterWrong / EA Forum posts → ``forum`` (``fm_post``); X /
  Twitter statuses → ``social`` (``x_post``); everything else → ``url`` with one ``url`` key.
* :func:`alternate_url_keys` ``(raw) -> tuple[str, ...]`` — **the same page's other keys**: forms
  :func:`url_key` cannot fold into one (:data:`ALTERNATE_URL_RULES`, e.g. an Alignment Forum post also at
  ``lesswrong.com``); :mod:`stack.identity`'s ``url_alternates`` adder stores them as extra ``url`` keys.

The URL-shaped extractors (:func:`arxiv_of_url`, :func:`doi_of_url`, :func:`pmid_of_url`,
:func:`fm_post_of_url`, :func:`x_status_of_url`) return raw ids; :mod:`stack.scholarly_records`'s
``normalize_*`` call them and stay the key-typing entry points (they own the normal forms).
v3's ``stack.collect.listing.norm_url`` (https, ``www.`` kept, every query parameter dropped) is retired.

``identity.canonical_url`` of a ``sources`` row is the **fetch form** (url-normalisation.md §2); identity and
source rules compare keys, never ``canonical_url``.
"""

from __future__ import annotations

import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------- constants (the rules as data)
#: Query parameters dropped from every URL (fetch and key form): click ids and mail/ads trackers.
TRACKING_PARAMS: frozenset[str] = frozenset({
    "fbclid", "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "twclid", "ttclid",
    "li_fat_id", "igshid", "igsh", "mc_cid", "mc_eid", "_hsenc", "_hsmi", "__hstc", "__hssc", "__hsfp",
    "hsctatracking", "mkt_tok", "oly_anon_id", "oly_enc_id", "vero_id", "vero_conv", "rb_clickid",
    "s_cid", "ref", "ref_src", "ref_url", "triedredirect", "isfreemail", "showwelcome", "cmpid",
    "wt.mc_id", "spm", "_ga", "_gl",
})
#: Parameter-name prefixes dropped everywhere (lower-cased names): ``utm_*``, Matomo/Piwik, HubSpot ads.
TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "pk_", "mtm_", "hsa_")
#: Parameters that are tracking only on these hosts (``www.``-less; a parent domain covers its
#: subdomains). ``s``/``t`` are X's share tokens but a search term / timestamp elsewhere.
HOST_TRACKING_PARAMS: Mapping[str, frozenset[str]] = {
    "x.com": frozenset({"s", "t", "ref_src", "ref_url", "src"}),
    "twitter.com": frozenset({"s", "t", "ref_src", "ref_url", "src"}),
    "youtube.com": frozenset({"si", "feature", "pp", "ab_channel"}),
    "youtu.be": frozenset({"si", "feature"}),
    "open.spotify.com": frozenset({"si", "nd", "context"}),
    "instagram.com": frozenset({"igshid", "igsh", "img_index"}),
    "linkedin.com": frozenset({"trk", "trackingid", "lipi", "midtoken", "midsig", "trkemail", "eid",
                               "original_referer", "rcm"}),
    "medium.com": frozenset({"source", "gi"}),
    "reddit.com": frozenset({"share_id", "rdt", "context"}),
    "substack.com": frozenset({"r", "s", "publication_id", "post_id", "sd", "token", "action"}),
    "facebook.com": frozenset({"mibextid", "rdid", "sfnsn"}),
    "tiktok.com": frozenset({"is_from_webapp", "sender_device", "_r", "_t"}),
}
#: Substack's share parameters, dropped on any host's ``/p/<slug>`` path (custom-domain Substacks
#: such as ``blog.redwoodresearch.org`` serve the same parameters).
SUBSTACK_POST_PARAMS: frozenset[str] = HOST_TRACKING_PARAMS["substack.com"]

#: Redirect wrappers: host → (path regex, the parameters holding the target, in order). A host
#: entry also covers its subdomains; ``google.<tld>`` is matched by :data:`_GOOGLE_HOST`.
REDIRECT_WRAPPERS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "google": (r"^/url$", ("q", "url")),
    "l.facebook.com": (r"^/l\.php$", ("u",)),
    "lm.facebook.com": (r"^/l\.php$", ("u",)),
    "m.facebook.com": (r"^/l\.php$", ("u",)),
    "l.instagram.com": (r"^/?$", ("u",)),
    "out.reddit.com": (r"^/", ("url",)),
    "youtube.com": (r"^/redirect$", ("q",)),
    "slack-redir.net": (r"^/link$", ("url",)),
    "t.umblr.com": (r"^/redirect$", ("z",)),
    "linkedin.com": (r"^/redir/redirect$", ("url",)),
    "safelinks.protection.outlook.com": (r"^/?$", ("url",)),
    "duckduckgo.com": (r"^/l/?$", ("uddg",)),
    "steamcommunity.com": (r"^/linkfilter/?$", ("url", "u")),
}
_GOOGLE_HOST = re.compile(r"^(?:www\.)?google\.(?:com|[a-z]{2,3}|com?\.[a-z]{2})$")
#: ``web.archive.org/web/<timestamp><flags>/<url>`` → ``<url>`` (the archived page).
_WAYBACK = re.compile(r"^/web/\d{1,14}[a-z_*]*/(.+)$", re.IGNORECASE)
#: archive.today's hosts (round 4 L4). ``/o/<id>/<url>`` is an outbound link inside a snapshot and
#: unwraps to ``<url>``; a bare snapshot ``/<id>`` is the archived copy itself and stays a url target.
ARCHIVE_TODAY_HOSTS: frozenset[str] = frozenset({
    "archive.is", "archive.ph", "archive.today", "archive.li", "archive.vn", "archive.md",
})
_ARCHIVE_TODAY = re.compile(r"^/o/[A-Za-z0-9]{1,16}/(.+)$")
#: Literal and percent-encoded whitespace (space, tab, LF, CR, no-break space) at either end of an
#: href, stripped before anything else (round 4 L4: paradigm3's hrefs end in ``%20``, which hid
#: X statuses and pages behind a key no other link has).
_EDGE_SPACE = re.compile(r"^(?:\s|%(?:20|09|0[aA]|0[dD]|[cC]2%[aA]0))+|(?:\s|%(?:20|09|0[aA]|0[dD]|[cC]2%[aA]0))+$")
#: A Google Doc (``/document[/u/<n>]/d/<id>/…``; not the ``/d/e/<id>/pub`` published form, an
#: HTML page): fetched as ``/document/d/<id>/export?format=txt`` (the page is UI chrome to the
#: http lane), keyed ``https://docs.google.com/document/d/<id>`` (round 4 L4).
_GDOC_PATH = re.compile(r"^/document/(?:u/\d+/)?d/([A-Za-z0-9_-]{20,})(?:/.*)?$")

#: Hosts where ``m.``/``mobile.`` serves the same content as the bare host (key form only).
MOBILE_HOSTS: frozenset[str] = frozenset({
    "youtube.com", "twitter.com", "x.com", "facebook.com", "reddit.com", "wikipedia.org", "imdb.com",
})
#: Mirror hosts folded onto the host they mirror (key form only).
HOST_ALIASES: Mapping[str, str] = {
    "twitter.com": "x.com", "fxtwitter.com": "x.com", "vxtwitter.com": "x.com", "fixupx.com": "x.com",
    "fixvx.com": "x.com", "nitter.net": "x.com",
    "old.reddit.com": "reddit.com", "np.reddit.com": "reddit.com", "new.reddit.com": "reddit.com",
    "greaterwrong.com": "lesswrong.com", "ea.greaterwrong.com": "forum.effectivealtruism.org",
    "youtube-nocookie.com": "youtube.com", "export.arxiv.org": "arxiv.org", "dx.doi.org": "doi.org",
    "ar5iv.org": "arxiv.org", "ar5iv.labs.arxiv.org": "arxiv.org",
    "edition.cnn.com": "cnn.com",
}
#: Identity query parameters kept in the key form, by host (parent domains cover subdomains).
#: YouTube's ``list`` is kept on ``/playlist`` only (:func:`_canonical`).
KEY_PARAMS: Mapping[str, frozenset[str]] = {
    "openreview.net": frozenset({"id"}),
    "youtube.com": frozenset({"v"}),
    "news.ycombinator.com": frozenset({"id"}),
    "journals.plos.org": frozenset({"id"}),
    "ssrn.com": frozenset({"abstract_id"}),
    "podcasts.apple.com": frozenset({"i"}),
    "play.google.com": frozenset({"id"}),
    "drive.google.com": frozenset({"id"}),
    "facebook.com": frozenset({"story_fbid", "id", "fbid", "v"}),
    "wikipedia.org": frozenset({"title", "oldid", "curid"}),
    "ieeexplore.ieee.org": frozenset({"arnumber"}),
    "dl.acm.org": frozenset({"id"}),
    "citeseerx.ist.psu.edu": frozenset({"doi"}),
    "mp.weixin.qq.com": frozenset({"__biz", "mid", "idx", "sn"}),
    "papers.nips.cc": frozenset({"paper_id"}),
    # Chinese hosts whose pages are addressed by the query (the Chinese-coverage work): CNKI article pages
    # (``kcms/detail/detail.aspx?dbcode=…&dbname=…&filename=…``; the kcms2 ``v=<token>`` is a session token, not
    # identity, and is dropped (D752): a ``kcms2/article/abstract?v=…`` link keys to its bare path, so two such links
    # share a key; prefer the detail.aspx or DOI form of a CNKI article), parameter names
    # matched case-insensitively (:data:`CASELESS_KEY_PARAM_HOSTS`: ``FileName``/``filename``); TC260 notices and
    # drafts for comment (``front/postDetail.html?id=…``, ``front/bzzqyjDetail.html?id=…&norm_id=…&recode_id=…``)
    # and its standards library (``tc260/bzzqyj/bzkdetail.shtml?id=…&type=…``: the ``id`` repeats across the
    # ``type`` lists, so ``type`` is identity there); the national standards portals
    # (``openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=…``, also linked as ``HCNO=…``: names case-insensitive;
    # ``std.samr.gov.cn/gb/search/gbDetailed?id=…``).
    "cnki.net": frozenset({"dbcode", "dbname", "filename"}),
    "tc260.org.cn": frozenset({"id", "norm_id", "recode_id", "type"}),
    "openstd.samr.gov.cn": frozenset({"hcno"}),
    "std.samr.gov.cn": frozenset({"id"}),
}
#: Hosts whose identity parameter names are compared and kept lower-cased (parent domains cover subdomains).
CASELESS_KEY_PARAM_HOSTS: frozenset[str] = frozenset({"cnki.net", "samr.gov.cn"})
#: WordPress post / page ids, kept on any host when numeric.
WORDPRESS_PARAMS: frozenset[str] = frozenset({"p", "page_id"})

#: Medium hosts outside ``*.medium.com`` whose post paths end in the 12-hex post id.
MEDIUM_HOSTS: frozenset[str] = frozenset({"medium.com", "towardsdatascience.com"})

#: DOI-in-path publishers: (host, path regex with the DOI as group 1, DOI prefix to prepend).
#: The generic Atypon form ``/doi/[abs|full|pdf|…/]10.…`` applies on any host (:data:`_ATYPON_DOI`).
DOI_PATH_PATTERNS: tuple[tuple[str, str, str], ...] = (
    ("link.springer.com",
     r"^/(?:article|chapter|book|referenceworkentry|protocol|content/pdf)/(10\.\d{4,9}/[^?#]+?)(?:\.pdf)?$", ""),
    ("frontiersin.org", r"^/articles/(10\.\d{4,9}/[^?#]+?)(?:/full|/abstract|/pdf)?$", ""),
    ("iopscience.iop.org", r"^/article/(10\.\d{4,9}/[^?#]+?)(?:/meta|/pdf|/ampdf)?$", ""),
    ("journals.aps.org", r"^/[a-z]+/(?:abstract|pdf|accepted)/(10\.\d{4,9}/[^?#]+)$", ""),
    ("biorxiv.org", r"^/content/(10\.1101/[\d.]+?)(?:v\d+)?(?:\.full(?:\.pdf)?|\.abstract|\.full-text)?$", ""),
    ("medrxiv.org", r"^/content/(10\.1101/[\d.]+?)(?:v\d+)?(?:\.full(?:\.pdf)?|\.abstract|\.full-text)?$", ""),
    ("jstor.org", r"^/stable/(?:pdf/)?(10\.\d{4,9}/[^?#]+?)(?:\.pdf)?$", ""),
    ("nature.com", r"^/articles/([a-z][a-z0-9.\-]*\d[a-z0-9.\-]*?)(?:\.pdf)?$", "10.1038/"),
)
_ATYPON_DOI = re.compile(
    r"^/doi/(?:(?:abs|full|pdf|epdf|pdfdirect|fullhtml|reader|book|epub|figure|suppl)/)?(10\.\d{4,9}/[^?#]+)$",
    re.IGNORECASE)

#: arXiv id grammar (new style ``YYMM.NNNN(N)``; old style ``archive(.SC)/YYMMNNN``), shared with
#: :mod:`stack.scholarly_records`.
ARXIV_ID = r"(\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})"
#: arXiv mirrors whose paper pages are addressed by the arXiv id (keyed as the arXiv paper).
ARXIV_MIRRORS = r"huggingface\.co/papers/|alphaxiv\.org/(?:abs|overview|pdf)/"
ARXIV_MIRROR_HOSTS: frozenset[str] = frozenset({"huggingface.co", "alphaxiv.org"})
#: arXiv URL forms, searched anywhere in a string: ``arxiv.org`` (any subdomain: ``www.``,
#: ``export.``, ``ar5iv.labs.``) or ``ar5iv.org``, ``abs|pdf|html|format|ps``, or a mirror
#: (:data:`ARXIV_MIRRORS`); an optional ``vN`` (group 2) and ``.pdf``.
ARXIV_URL_RE = re.compile(
    r"(?:(?:arxiv\.org|ar5iv\.org)/(?:abs|pdf|html|format|ps)/|" + ARXIV_MIRRORS + r")"
    + ARXIV_ID + r"(?:v(\d+))?(?:\.pdf)?(?!\w)", re.IGNORECASE)

FORUM_HOSTS: frozenset[str] = frozenset({
    "lesswrong.com", "alignmentforum.org", "greaterwrong.com", "forum.effectivealtruism.org",
    "ea.greaterwrong.com",
})
_FM_POST_PATH = re.compile(r"^/(?:posts|s/[A-Za-z0-9]+/p)/([A-Za-z0-9]+)(?:/[^/]*)?/?$")

X_HOSTS: frozenset[str] = frozenset({
    "x.com", "twitter.com", "fxtwitter.com", "vxtwitter.com", "fixupx.com", "fixvx.com", "nitter.net",
})
_X_STATUS_PATH = re.compile(r"^/(?:[A-Za-z0-9_]{1,30}/status(?:es)?|i/web/status|i/status|statuses)/(\d{1,25})(?:/.*)?$")
#: First path segments of X that are not profiles.
X_RESERVED: frozenset[str] = frozenset({
    "home", "i", "intent", "search", "explore", "hashtag", "settings", "notifications", "messages", "share",
    "login", "signup", "tos", "privacy", "compose", "statuses",
})
PUBMED_RE = re.compile(r"^/(?:pubmed/)?(\d{1,9})/?$")

#: Kind hints of :func:`link_keys` (``share`` added for share intents, which admission drops;
#: ``shop`` for shops and book stores, which ``collect.links`` rejects by rule, round 4 (L2)).
KINDS: tuple[str, ...] = ("paper", "forum_post", "tweet", "repo", "video", "wiki", "news", "gov",
                          "social_profile", "share", "shop", "pdf", "other")
SHARE_PATHS: tuple[tuple[str, str], ...] = (
    ("x.com", r"^/(?:intent|share)"), ("twitter.com", r"^/(?:intent|share)"),
    ("facebook.com", r"^/(?:sharer|share|dialog/share)"), ("linkedin.com", r"^/(?:sharearticle|shareArticle|share)"),
    ("reddit.com", r"^/submit"), ("news.ycombinator.com", r"^/submitlink"), ("t.me", r"^/share"),
    ("pinterest.com", r"^/pin/create"), ("bsky.app", r"^/intent"), ("api.whatsapp.com", r"^/send"),
    ("wa.me", r"^/"), ("mastodonshare.com", r"^/"), ("threads.net", r"^/intent"),
)
REPO_HOSTS: frozenset[str] = frozenset({
    "github.com", "gitlab.com", "bitbucket.org", "codeberg.org", "huggingface.co", "gist.github.com",
    "raw.githubusercontent.com", "sr.ht", "git.sr.ht",
})
VIDEO_HOSTS: frozenset[str] = frozenset({"youtube.com", "youtu.be", "vimeo.com", "tiktok.com", "twitch.tv",
                                         "dailymotion.com", "loom.com"})
WIKI_HOSTS: frozenset[str] = frozenset({"wikipedia.org", "wikimedia.org", "wiktionary.org", "wikidata.org",
                                        "arbital.com", "stampy.ai", "aisafety.info"})
#: Forum wiki/tag paths (the forums' wikis), counted as ``wiki``.
FORUM_WIKI_PATH = re.compile(r"^/(?:tag|w|wiki|topics)/")
NEWS_HOSTS: frozenset[str] = frozenset({
    "nytimes.com", "wsj.com", "ft.com", "bloomberg.com", "reuters.com", "theguardian.com", "bbc.co.uk", "bbc.com",
    "washingtonpost.com", "economist.com", "axios.com", "theverge.com", "techcrunch.com", "wired.com", "cnbc.com",
    "cnn.com", "politico.com", "politico.eu", "theinformation.com", "semafor.com", "apnews.com", "nbcnews.com",
    "vox.com", "time.com", "theatlantic.com", "newyorker.com", "arstechnica.com", "businessinsider.com",
    "fortune.com", "forbes.com", "latimes.com", "npr.org", "foxnews.com", "independent.co.uk", "telegraph.co.uk",
    "nypost.com", "thehill.com", "venturebeat.com", "404media.co", "platformer.news", "scmp.com", "lemonde.fr", "spiegel.de", "nikkei.com", "asia.nikkei.com", "cbsnews.com", "abcnews.go.com", "usatoday.com",
    "newsweek.com", "zdnet.com", "engadget.com", "theregister.com", "fastcompany.com", "qz.com", "slate.com",
})
GOV_SUFFIXES: tuple[str, ...] = (".gov", ".gov.uk", ".europa.eu", ".gov.au", ".gc.ca", ".gouv.fr", ".bund.de",
                                 ".mil", ".gov.cn", ".go.jp", ".gov.in", ".gov.sg")
#: Shops and book stores (``www.``-less; a parent covers its subdomains), hinted ``shop``.
SHOP_HOSTS: frozenset[str] = frozenset({
    "amazon.com", "amazon.co.uk", "amazon.de", "amazon.fr", "amazon.ca", "amazon.in", "amazon.co.jp", "amzn.to",
    "a.co", "ebay.com", "etsy.com", "bookshop.org", "barnesandnoble.com", "waterstones.com", "goodreads.com",
    "aliexpress.com", "walmart.com", "bestbuy.com",
})
#: Research organisations on government domains, hinted ``other`` (their posts are research, D195).
GOV_RESEARCH_HOSTS: frozenset[str] = frozenset({"aisi.gov.uk", "nist.gov"})
SOCIAL_PROFILE_PATHS: tuple[tuple[str, str], ...] = (
    ("bsky.app", r"^/profile/[^/]+/?$"), ("linkedin.com", r"^/(?:in|company)/[^/]+/?$"),
    ("threads.net", r"^/@[^/]+/?$"), ("instagram.com", r"^/[^/]+/?$"), ("mastodon.social", r"^/@[^/]+/?$"),
    ("facebook.com", r"^/[^/.]+/?$"),
)
#: Url-group hosts whose pages are papers without an arXiv/DOI/PMID key in the URL.
PAPER_HOSTS: frozenset[str] = frozenset({
    "openreview.net", "aclanthology.org", "proceedings.mlr.press", "papers.nips.cc", "proceedings.neurips.cc",
    "ojs.aaai.org", "openaccess.thecvf.com", "ieeexplore.ieee.org", "semanticscholar.org", "ssrn.com",
    "researchgate.net", "europepmc.org", "jmlr.org", "academic.oup.com", "sciencedirect.com", "cambridge.org",
    "mdpi.com", "philpapers.org", "scholar.archive.org", "ijcai.org", "dl.acm.org", "pmc.ncbi.nlm.nih.gov",
    "journals.plos.org", "iclr.cc", "icml.cc", "neurips.cc", "aaai.org", "osf.io", "psyarxiv.com", "zenodo.org",
    "ncbi.nlm.nih.gov",
})
_PAPER_PATH = {"semanticscholar.org": r"^/paper/", "researchgate.net": r"^/publication/",
               "iclr.cc": r"^/virtual/\d{4}/(?:poster|oral|paper)", "icml.cc": r"^/virtual/\d{4}/(?:poster|oral)",
               "neurips.cc": r"^/virtual/\d{4}/(?:poster|oral)", "osf.io": r"^/preprints/",
               "ncbi.nlm.nih.gov": r"^/pmc/"}
_MEDIUM_ID = re.compile(r"-([0-9a-f]{12})$")
_DIGITS = re.compile(r"^\d+$")
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
_SCHEME_SLASH = re.compile(r"^(https?):/(?!/)", re.IGNORECASE)


# ---------------------------------------------------------------- helpers
def _bare(host: str) -> str:
    """``host`` lower case without a leading ``www.``."""
    host = host.lower().rstrip(".")
    return host.removeprefix("www.")


def _covered(host: str, table: Mapping[str, Any] | frozenset[str]) -> str | None:
    """The entry of ``table`` naming ``host`` or a parent domain of it, else ``None``."""
    parts = host.split(".")
    for i in range(len(parts) - 1):
        cand = ".".join(parts[i:])
        if cand in table:
            return cand
    return None


def _is_tracking(name: str, host: str, path: str) -> bool:
    n = name.lower()
    if n in TRACKING_PARAMS or n.startswith(TRACKING_PREFIXES):
        return True
    h = _covered(host, HOST_TRACKING_PARAMS)
    if h is not None and n in HOST_TRACKING_PARAMS[h]:
        return True
    return path.startswith("/p/") and n in SUBSTACK_POST_PARAMS


def _split(raw: str, base: str | None) -> urllib.parse.SplitResult | None:
    s = _EDGE_SPACE.sub("", raw or "").replace("\t", "").replace("\n", "").replace("\r", "")
    if not s:
        return None
    s = s.replace(" ", "%20")
    if base:
        s = urllib.parse.urljoin(base, s)
    elif s.startswith("//"):
        s = "https:" + s
    try:
        p = urllib.parse.urlsplit(s)
        p.port  # noqa: B018 - a malformed port raises here
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    return p


def _unwrap(p: urllib.parse.SplitResult) -> str | None:
    """The target of a redirect wrapper or a Wayback URL, else ``None``."""
    host = _bare(p.hostname or "")
    if host == "web.archive.org":
        m = _WAYBACK.match(p.path)
        if m:
            inner = _SCHEME_SLASH.sub(r"\1://", m.group(1))
            return inner + (f"?{p.query}" if p.query else "")
        return None
    if host in ARCHIVE_TODAY_HOSTS:
        m = _ARCHIVE_TODAY.match(p.path)
        if m:
            inner = _SCHEME_SLASH.sub(r"\1://", m.group(1))
            return inner + (f"?{p.query}" if p.query else "")
        return None
    key = "google" if _GOOGLE_HOST.match(host) else _covered(host, REDIRECT_WRAPPERS)
    if key is None:
        return None
    path_re, params = REDIRECT_WRAPPERS[key]
    if not re.match(path_re, p.path):
        return None
    q = urllib.parse.parse_qs(p.query)
    for name in params:
        if q.get(name):
            return q[name][0]
    return None


def _netloc(p: urllib.parse.SplitResult, host: str) -> str:
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = p.port
    default = {"http": 80, "https": 443}.get(p.scheme.lower())
    return host if port is None or port == default else f"{host}:{port}"


# ---------------------------------------------------------------- fetch form
#: A Wayback Machine capture of the original bytes: ``web.archive.org/web/<14-digit timestamp>id_/<http(s) URL>``.
_WAYBACK_CAPTURE = re.compile(r"^https?://web\.archive\.org/web/(\d{14})id_/(https?://\S+)$", re.IGNORECASE)


def wayback_capture(raw: str | None) -> str | None:
    """``raw`` when it names one Wayback capture's original bytes (``https://web.archive.org/web/<14-digit
    timestamp>id_/<http(s) URL>``; scheme and host lower case, the rest as written), else ``None``. Such a URL is a
    pin: :func:`fetch_url` unwraps it to the live page like any Wayback URL (the merging key is the live page's),
    and a caller that wants the pinned bytes, ``collect.manual`` for a hand-added source the live host does not
    serve, fetches this form instead."""
    m = _WAYBACK_CAPTURE.match((raw or "").strip())
    return f"https://web.archive.org/web/{m.group(1)}id_/{m.group(2)}" if m else None


def fetch_url(raw: str | None, base: str | None = None) -> str | None:
    """What a lane fetches (module docstring); ``None`` when ``raw`` (resolved against ``base``) is
    not an http(s) URL."""
    p = _split(raw or "", base)
    for _ in range(4):  # nested wrappers (a Google redirect to a Wayback copy …)
        if p is None:
            return None
        inner = _unwrap(p)
        if inner is None:
            break
        q = _split(inner, None)
        if q is None:
            break  # a wrapper of something that is not a web URL: keep the wrapper
        p = q
    assert p is not None
    host = (p.hostname or "").lower().rstrip(".")
    bhost = _bare(host)
    if bhost == "docs.google.com" and (gdoc := _GDOC_PATH.match(p.path)):
        return f"https://docs.google.com/document/d/{gdoc.group(1)}/export?format=txt"
    # filter the raw ``name=value`` items: the kept ones keep their original encoding
    kept = [x for x in p.query.split("&")
            if x and not _is_tracking(urllib.parse.unquote_plus(x.split("=", 1)[0]), bhost, p.path)]
    query = "&".join(kept)
    return urllib.parse.urlunsplit((p.scheme.lower(), _netloc(p, host), p.path, query, ""))


# ---------------------------------------------------------------- url-shaped extractors
def arxiv_of_url(text: str) -> tuple[str, int | None] | None:
    """``(raw arXiv id, version or None)`` from an arXiv / ar5iv URL anywhere in ``text``."""
    m = ARXIV_URL_RE.search(text or "")
    if m is None:
        return None
    return m.group(1), int(m.group(2)) if m.group(2) else None


def _doi_path(host: str, path: str, query: str) -> str:
    path = urllib.parse.unquote(path)
    m = _ATYPON_DOI.match(path)
    if m:
        return m.group(1)
    for h, pat, prefix in DOI_PATH_PATTERNS:
        if host == h or host.endswith("." + h):
            m = re.match(pat, path, re.IGNORECASE)
            if m:
                return prefix + m.group(1)
    if host == "journals.plos.org":
        ids = urllib.parse.parse_qs(query).get("id")
        if ids:
            return ids[0]
    return ""


def doi_of_url(raw: str) -> str:
    """A raw DOI from a resolver URL (``doi.org``, ``dx.doi.org``) or a DOI-in-path publisher URL
    (:data:`DOI_PATH_PATTERNS`, the Atypon ``/doi/`` form, PLOS ``?id=``), else ``''``. Not
    validated: :func:`stack.scholarly_records.normalize_doi` does that."""
    p = _split(raw, None)
    if p is None:
        return ""
    host = _bare(p.hostname or "")
    if host in ("doi.org", "dx.doi.org"):
        return urllib.parse.unquote(p.path.lstrip("/"))
    return _doi_path(host, p.path, p.query)


def pmid_of_url(raw: str) -> str:
    """A PubMed id from a ``pubmed.ncbi.nlm.nih.gov/<n>`` or ``ncbi.nlm.nih.gov/pubmed/<n>`` URL."""
    p = _split(raw, None)
    if p is None:
        return ""
    host = _bare(p.hostname or "")
    if host == "pubmed.ncbi.nlm.nih.gov" or (host == "ncbi.nlm.nih.gov" and p.path.startswith("/pubmed/")):
        m = PUBMED_RE.match(p.path)
        return m.group(1) if m else ""
    return ""


def fm_post_of_url(raw: str) -> str:
    """A ForumMagnum post id from a forum post URL (``/posts/<id>[/slug]``, ``/s/<seq>/p/<id>``;
    ``?commentId=`` and fragments point at the post), else ``''``."""
    p = _split(raw, None)
    if p is None or _bare(p.hostname or "") not in FORUM_HOSTS:
        return ""
    m = _FM_POST_PATH.match(p.path)
    return m.group(1) if m else ""


def x_status_of_url(raw: str) -> str:
    """An X post id from a status URL on X, Twitter, ``mobile.`` and the mirrors
    (``/<user>/status/<id>``, ``/i/web/status/<id>``, ``/i/status/<id>``), else ``''``."""
    p = _split(raw, None)
    if p is None:
        return ""
    host = _bare(p.hostname or "")
    host = host.split(".", 1)[1] if host.startswith(("mobile.", "m.")) else host
    if host not in X_HOSTS:
        return ""
    m = _X_STATUS_PATH.match(p.path)
    return m.group(1) if m else ""


def x_profile_of_url(raw: str) -> str:
    """The lower-cased handle of an X profile URL (one path segment, not reserved), else ``''``."""
    p = _split(raw, None)
    if p is None:
        return ""
    host = _bare(p.hostname or "")
    host = host.split(".", 1)[1] if host.startswith(("mobile.", "m.")) else host
    if host not in X_HOSTS:
        return ""
    seg = p.path.strip("/")
    if not seg or "/" in seg or seg.lower() in X_RESERVED or not re.match(r"^[A-Za-z0-9_]{1,30}$", seg):
        return ""
    return seg.lower()


# ---------------------------------------------------------------- key form
def _key_host(host: str) -> str:
    h = _bare(host)
    for pre in ("m.", "mobile."):
        if h.startswith(pre) and _covered(h[len(pre):], MOBILE_HOSTS):
            h = h[len(pre):]
            break
    if h.endswith(".m.wikipedia.org"):
        h = h[: -len(".m.wikipedia.org")] + ".wikipedia.org"
    return HOST_ALIASES.get(h, h)


def _key_query(host: str, path: str, query: str) -> str:
    allowed = KEY_PARAMS.get(_covered(host, KEY_PARAMS) or "", frozenset())
    caseless = _covered(host, CASELESS_KEY_PARAM_HOSTS) is not None
    kept = []
    for k, v in urllib.parse.parse_qsl(query, keep_blank_values=False):
        if caseless:
            k = k.lower()
        if k in allowed or (k in WORDPRESS_PARAMS and _DIGITS.match(v)):
            kept.append((k, v))
    if host == "youtube.com" and path == "/playlist":
        kept += [(k, v) for k, v in urllib.parse.parse_qsl(query) if k == "list"]
    return urllib.parse.urlencode(sorted(set(kept)), safe="/:@")


def _github(host: str, path: str) -> str:
    segs = path.split("/")  # ['', owner, repo, ...]
    if len(segs) < 3 or not segs[1] or not segs[2]:
        return path.lower() if len(segs) == 2 else path
    owner, repo = segs[1].lower(), segs[2].lower()
    if host == "github.com":
        repo = repo.removesuffix(".git")
        rest = segs[3:]
        if len(rest) == 2 and rest[0] == "tree":
            rest = []  # /tree/<ref> at the root is the repo
        elif len(rest) >= 3 and rest[0] == "blob":
            rest = ["blob", "-", *rest[2:]]  # one file path → one key, whatever the ref
        return "/".join(["", owner, repo, *rest])
    # raw.githubusercontent.com/<owner>/<repo>/<ref>/<file path>: the ref folded (main/HEAD/sha)
    rest = segs[3:]
    if len(rest) >= 2:
        rest = ["-", *rest[1:]]
    return "/".join(["", owner, repo, *rest])


def _canonical(host: str, path: str, query: str) -> tuple[str, str, str] | None:
    """Domain canonical forms: ``(host, path, query)`` of the key, or ``None`` for the generic rule."""
    if host == "arxiv.org" or host in ARXIV_MIRROR_HOSTS:
        a = arxiv_of_url(f"{host}{path}")
        if a is not None:
            from stack.scholarly_records import (
                normalize_arxiv,  # lazy: scholarly_records imports this module
            )
            aid = normalize_arxiv(a[0])
            if aid:
                return "arxiv.org", f"/abs/{aid}", ""
    if host == "doi.org":
        from stack.scholarly_records import normalize_doi

        doi = normalize_doi("https://doi.org" + path)
        if doi:
            return "doi.org", "/" + doi, ""
    if host == "x.com":
        m = _X_STATUS_PATH.match(path)
        if m:
            return "x.com", f"/i/status/{m.group(1)}", ""
        seg = path.strip("/")
        if seg and "/" not in seg and seg.lower() not in X_RESERVED:
            return "x.com", "/" + seg.lower(), ""
    if host in ("youtube.com", "youtu.be"):
        vid = ""
        if host == "youtu.be":
            vid = path.strip("/").split("/")[0]
        else:
            m = re.match(r"^/(?:shorts|embed|live|v|e)/([^/]+)", path)
            if m:
                vid = m.group(1)
            elif path == "/watch":
                vid = urllib.parse.parse_qs(query).get("v", [""])[0]
        if vid and _YT_ID.match(vid):
            return "youtube.com", "/watch", urllib.parse.urlencode({"v": vid})
    if host == "openreview.net" and path in ("/forum", "/pdf", "/attachment"):
        oid = urllib.parse.parse_qs(query).get("id", [""])[0]
        if oid:
            return "openreview.net", "/forum", urllib.parse.urlencode({"id": oid})
    if host == "open.substack.com":
        m = re.match(r"^/pub/([^/]+)/p/([^/]+)", path)
        if m:
            return f"{m.group(1).lower()}.substack.com", f"/p/{m.group(2)}", ""
    if host == "substack.com":
        m = re.match(r"^/@([^/]+)/p/([^/]+)", path)
        if m:
            return f"{m.group(1).lower()}.substack.com", f"/p/{m.group(2)}", ""
    if host.endswith(".substack.com"):
        m = re.match(r"^/p/([^/]+)", path)
        if m:
            return host, f"/p/{m.group(1)}", ""
    if host in MEDIUM_HOSTS or host.endswith(".medium.com"):
        last = path.rstrip("/").rsplit("/", 1)[-1]
        m = _MEDIUM_ID.search(last)
        if m:
            return "medium.com", f"/p/{m.group(1)}", ""
        if re.match(r"^/p/[0-9a-f]{12}$", path):
            return "medium.com", path, ""
    if host in ("github.com", "raw.githubusercontent.com"):
        return host, _github(host, path), ""
    if host in FORUM_HOSTS or host in ("lesswrong.com", "forum.effectivealtruism.org"):
        m = _FM_POST_PATH.match(path)
        if m:
            return host, f"/posts/{m.group(1)}", ""
    if host == "docs.google.com":
        m = _GDOC_PATH.match(path)
        if m:
            return host, f"/document/d/{m.group(1)}", ""
    return None


def url_key(raw: str | None) -> str:
    """The ``url`` key of ``raw`` (module docstring); ``''`` when it is not an absolute http(s) URL."""
    f = fetch_url(raw or "")
    if f is None:
        return ""
    p = urllib.parse.urlsplit(f)
    host = _key_host(p.hostname or "")
    path = p.path.rstrip("/")
    canon = _canonical(host, path, p.query)
    if canon is not None:
        host, path, query = canon
    else:
        query = _key_query(host, path, p.query)
    return urllib.parse.urlunsplit(("https", _netloc(p, host), path, query, ""))


# ---------------------------------------------------------------- alternate keys
def _alt_forummagnum(key: urllib.parse.SplitResult) -> list[str]:
    """LessWrong and the Alignment Forum serve one ForumMagnum database: an AF post is also at
    ``lesswrong.com/posts/<id>`` (GreaterWrong is folded by :data:`HOST_ALIASES`)."""
    hosts = ("lesswrong.com", "alignmentforum.org")
    m = _FM_POST_PATH.match(key.path) if key.hostname in hosts else None
    return [f"https://{h}/posts/{m.group(1)}" for h in hosts if m and h != key.hostname]


#: Moved post paths (D772): sites that moved their posts to another path or host and redirect the old URL, so a
#: linkpost written before the move names the post's former address. Per family the key-form prefixes (``url_key``
#: form, ending in ``/``) under which one post is known by the same last path segment; the first is the current form.
#: Each family was checked against the documents head of 2026-10-23 (forum linkposts on a former form whose post the
#: lab channel holds on the current one, text copies of each other; far.ai and epochai.org redirects checked live on
#: 2026-10-30).
MOVED_POST_PATHS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("openai_index", ("https://openai.com/index/", "https://openai.com/blog/", "https://openai.com/research/")),
    ("redwood_custom_domain", ("https://blog.redwoodresearch.org/p/", "https://redwoodresearch.substack.com/p/")),
    ("apollo_science", ("https://apolloresearch.ai/science/", "https://apolloresearch.ai/blog/")),
    ("metr_renamed", ("https://metr.org/blog/", "https://evals.alignment.org/blog/")),
    ("far_blog", ("https://far.ai/blog/", "https://far.ai/news/")),
    ("aisi_blog", ("https://aisi.gov.uk/blog/", "https://aisi.gov.uk/work/")),
    ("deepmind_blog", ("https://deepmind.google/blog/", "https://deepmind.google/discover/blog/",
                       "https://deepmind.com/blog/")),
    ("epoch_publications", ("https://epoch.ai/publications/", "https://epoch.ai/blog/", "https://epochai.org/blog/")),
)


def _alt_moved_paths(key: urllib.parse.SplitResult) -> list[str]:
    """A post under one form of a :data:`MOVED_POST_PATHS` family → the same last path segment under its other forms.
    Only a single non-empty segment after the prefix counts (a listing page or a deeper path is no post of the family)."""
    k = urllib.parse.urlunsplit(key)
    for _name, forms in MOVED_POST_PATHS:
        for f in forms:
            slug = k[len(f):] if k.startswith(f) else ""
            if slug and "/" not in slug and not key.query:
                return [g + slug for g in forms if g != f]
    return []


#: Alternate-URL rules (round 5, T-ID; :mod:`stack.identity`): ``(name, fn(key form) -> other key forms of the
#: same page)``. A rule names forms that :func:`url_key` cannot fold into one (both hosts serve the page, or the old
#: one redirects: folding would change stored keys, an alternate only adds one); forms it can fold (arXiv
#: ``abs``/``pdf``/``html``, GreaterWrong, ``open.substack.com``) are folded there.
ALTERNATE_URL_RULES: tuple[tuple[str, Callable[[urllib.parse.SplitResult], list[str]]], ...] = (
    ("forummagnum_hosts", _alt_forummagnum),
    ("moved_post_paths", _alt_moved_paths),
)


def alternate_url_keys(raw: str | None) -> tuple[str, ...]:
    """The other ``url`` keys of the page ``raw`` names (:data:`ALTERNATE_URL_RULES`), without its own key."""
    key = url_key(raw)
    if not key:
        return ()
    p = urllib.parse.urlsplit(key)
    out = [a for _, rule in ALTERNATE_URL_RULES for a in rule(p)]
    return tuple(dict.fromkeys(a for a in out if a != key))


# ---------------------------------------------------------------- link targets
@dataclass(frozen=True)
class LinkTarget:
    """What one link points at: identity ``group`` (``paper``/``forum``/``social``/``url``), typed
    ``keys`` (``(kind, value)``, :func:`stack.scholarly_records.keys_for` order), the ``fetch_url``,
    a ``kind`` hint (:data:`KINDS`) and ``attributes`` (``arxiv_version``: int)."""

    group: str
    keys: tuple[tuple[str, str], ...]
    fetch_url: str
    kind: str
    attributes: Mapping[str, Any] = field(default_factory=dict)


def _host_path_match(host: str, path: str, table: tuple[tuple[str, str], ...]) -> bool:
    return any((host == h or host.endswith("." + h)) and re.match(pat, path, re.IGNORECASE)
               for h, pat in table)


def kind_hint(raw: str) -> str:
    """The kind hint of a url-group link (:data:`KINDS`, minus the keyed kinds)."""
    f = fetch_url(raw)
    if f is None:
        return "other"
    p = urllib.parse.urlsplit(f)
    host = _bare(p.hostname or "")
    path = p.path
    if _host_path_match(host, path, SHARE_PATHS):
        return "share"
    if x_profile_of_url(f) or _host_path_match(host, path, SOCIAL_PROFILE_PATHS):
        return "social_profile"
    if _covered(host, REPO_HOSTS):
        return "repo"
    if _covered(_key_host(host), VIDEO_HOSTS):
        return "video"
    if _covered(host, WIKI_HOSTS) or (host in FORUM_HOSTS and FORUM_WIKI_PATH.match(path)):
        return "wiki"
    if _covered(host, NEWS_HOSTS):
        return "news"
    if host.endswith(GOV_SUFFIXES) and not _covered(host, GOV_RESEARCH_HOSTS):
        return "gov"
    if _covered(host, SHOP_HOSTS):
        return "shop"
    ph = _covered(host, PAPER_HOSTS)
    if ph is not None and (ph not in _PAPER_PATH or re.match(_PAPER_PATH[ph], path)):
        return "paper"
    if path.lower().endswith(".pdf"):
        return "pdf"
    return "other"


def link_keys(raw: str | None, base: str | None = None) -> LinkTarget | None:
    """What ``raw`` (resolved against ``base``) points at (module docstring); ``None`` when it is
    not an http(s) URL."""
    from stack import (
        scholarly_records as sr,  # lazy: scholarly_records imports this module
    )

    f = fetch_url(raw or "", base)
    if f is None:
        return None
    a = arxiv_of_url(f)
    if a is not None and (aid := sr.normalize_arxiv(a[0])):
        attrs = {"arxiv_version": a[1]} if a[1] is not None else {}
        return LinkTarget("paper", _pairs(sr.keys_for([("arxiv", aid)])), f, "paper", attrs)
    doi = sr.normalize_doi(doi_of_url(f))
    if doi:
        return LinkTarget("paper", _pairs(sr.keys_for([("doi", doi)])), f, "paper")
    pmid = sr.normalize_pmid(pmid_of_url(f))
    if pmid:
        return LinkTarget("paper", (("pmid", pmid),), f, "paper")
    post = fm_post_of_url(f)
    if post:
        return LinkTarget("forum", (("fm_post", post),), f, "forum_post")
    status = x_status_of_url(f)
    if status:
        return LinkTarget("social", (("x_post", status),), f, "tweet")
    return LinkTarget("url", (("url", url_key(f)),), f, kind_hint(f))


def _pairs(keys: Any) -> tuple[tuple[str, str], ...]:
    return tuple((k["kind"], k["value"]) for k in keys)


__all__ = ["ALTERNATE_URL_RULES", "ARCHIVE_TODAY_HOSTS", "MOVED_POST_PATHS", "ARXIV_ID", "CASELESS_KEY_PARAM_HOSTS", "ARXIV_URL_RE", "DOI_PATH_PATTERNS", "HOST_TRACKING_PARAMS", "KEY_PARAMS", "KINDS",
           "REDIRECT_WRAPPERS", "TRACKING_PARAMS", "TRACKING_PREFIXES", "LinkTarget", "alternate_url_keys", "arxiv_of_url",
           "doi_of_url", "fetch_url", "fm_post_of_url", "kind_hint", "link_keys", "pmid_of_url", "url_key", "wayback_capture",
           "x_profile_of_url", "x_status_of_url"]
