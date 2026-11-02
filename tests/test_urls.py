"""``stack.urls`` (track U, round 4): the fetch form, the ``url`` key form and link targets, table-driven
over real URL shapes (``docs/design/link-crawling.md`` §3.4's hazards, the dev store's collectors and
the linking documents measured there). One table per entry point; the key-typing entry points of
:mod:`stack.scholarly_records` that gained URL forms are covered in the link table (their values are
the typed keys) and in ``tests/test_scholarly_records.py``.
"""

from __future__ import annotations

import pytest

from stack import scholarly_records as sr
from stack.urls import fetch_url, link_keys, url_key, wayback_capture

#: Google Docs URLs are built from these (synthetic document ids; no link to a real shared document, D796).
GDOC = "https://docs.google.com/" + "document"
GFORM = "https://docs.google.com/" + "forms"
DOC_A = "1" + "SyntheticDocA" * 3 + "x" * 4
DOC_B = "1-" + "SyntheticDocB" * 3 + "y" * 3
DOC_C = "1" + "SyntheticDocC" * 3 + "z" * 4
DOC_PUB, FORM = "2PACX-1vQ" + "synthetic" * 3, "1FAIpQLS" + "synthetic"

# ---------------------------------------------------------------- fetch form: what a lane fetches
FETCH = [
    # fragment and tracking dropped, every other parameter kept in its original encoding
    ("https://openai.com/index/x/?utm_source=twitter&utm_medium=social#top", "https://openai.com/index/x/"),
    ("https://example.org/a?b=1&fbclid=XYZ&c=%2F2", "https://example.org/a?b=1&c=%2F2"),
    ("https://thezvi.substack.com/p/ai-150?r=4ab&triedRedirect=true&utm_campaign=post",
     "https://thezvi.substack.com/p/ai-150"),
    ("https://blog.redwoodresearch.org/p/some-post?publication_id=123&post_id=456&isFreemail=true",
     "https://blog.redwoodresearch.org/p/some-post"),
    ("https://x.com/AnthropicAI/status/1840000000000000000?s=46&t=AbC", "https://x.com/AnthropicAI/status/1840000000000000000"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s&si=xyz", "https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=42s"),
    ("https://example.org/search?s=alignment&t=7", "https://example.org/search?s=alignment&t=7"),  # s/t: X only
    ("https://api.example.org/x?ref=main", "https://api.example.org/x"),
    # redirect wrappers unwrapped (§3.4), nested ones too
    ("https://www.google.com/url?q=https://arxiv.org/abs/2310.01405&sa=D&source=docs",
     "https://arxiv.org/abs/2310.01405"),
    ("https://l.facebook.com/l.php?u=https%3A%2F%2Fexample.org%2Fpost%3Futm_source%3Dfb&h=AT0",
     "https://example.org/post"),
    ("https://out.reddit.com/t3_abc?url=https%3A%2F%2Fexample.org%2Fa&token=x", "https://example.org/a"),
    ("https://web.archive.org/web/20230101000000/https://www.example.org/old/page/",
     "https://www.example.org/old/page/"),
    ("https://web.archive.org/web/2023id_/http://example.org/a?b=1", "http://example.org/a?b=1"),
    ("https://www.google.co.uk/url?q=https://web.archive.org/web/2020/https://example.org/x",
     "https://example.org/x"),
    # trailing encoded whitespace on an href (round 4 L4; paradigm3's hrefs end in `%20`), also inside a query
    ("https://x.com/1a3orn/status/2105319756270329993%20", "https://x.com/1a3orn/status/2105319756270329993"),
    ("https://generality.org/blog/posts/exploitbench-harnesses/%20%0A ",
     "https://generality.org/blog/posts/exploitbench-harnesses/"),
    ("https://example.org/a%20b/%0a", "https://example.org/a%20b/"),  # an inner %20 is content
    # archive.today `/o/<id>/<url>` wrappers (archive.is/.ph/.today/.li/.vn/.md); a bare snapshot stays
    ("https://archive.is/o/lhYzZ/https://www.hacktron.ai/blog/hacking-openai",
     "https://www.hacktron.ai/blog/hacking-openai"),
    ("https://archive.ph/o/d5p19/https://www.nytimes.com/2026/09/03/technology/x.html?smid=1&utm_source=tw",
     "https://www.nytimes.com/2026/09/03/technology/x.html?smid=1"),
    ("https://archive.today/o/AbC12/https:/example.org/a/", "https://example.org/a/"),
    ("https://archive.md/o/AbC12/ftp://example.org/a", "https://archive.md/o/AbC12/ftp://example.org/a"),
    ("https://archive.ph/yLnGK", "https://archive.ph/yLnGK"),
    # Google Docs: the plain-text export (the page itself is UI chrome to the http lane)
    (f"{GDOC}/d/{DOC_A}/edit?usp=sharing",
     f"{GDOC}/d/{DOC_A}/export?format=txt"),
    (f"{GDOC}/u/0/d/{DOC_B}/edit?tab=t.0%20#heading=h.x",
     f"{GDOC}/d/{DOC_B}/export?format=txt"),
    (f"{GDOC}/d/e/{DOC_PUB}/pub",
     f"{GDOC}/d/e/{DOC_PUB}/pub"),  # published: an html page
    (f"{GFORM}/d/e/{FORM}/viewform", f"{GFORM}/d/e/{FORM}/viewform"),
    # case, ports, credentials, relative links
    ("HTTPS://Alignment.Anthropic.COM:443/2025/Post", "https://alignment.anthropic.com/2025/Post"),
    ("http://user:pw@example.org:8080/a", "http://example.org:8080/a"),
    ("//cdn.example.org/a.pdf", "https://cdn.example.org/a.pdf"),
    # not a web URL
    ("mailto:someone@example.org", None),
    ("javascript:void(0)", None),
    ("", None),
]


@pytest.mark.parametrize("raw, want", FETCH)
def test_fetch_url(raw: str, want: str | None) -> None:
    assert fetch_url(raw) == want


def test_fetch_url_resolves_against_the_linking_page() -> None:
    base = "https://www.lesswrong.com/posts/abc123/some-post"
    assert fetch_url("../xyz789?commentId=1#c", base) == "https://www.lesswrong.com/posts/xyz789?commentId=1"
    assert fetch_url("#footnote-1", base) == base
    assert fetch_url("mailto:x@y.z", base) is None


# ---------------------------------------------------------------- key form: what merges
KEY = [
    # www., trailing slash, scheme, case of the host (the 9 www. pairs of §3.4)
    ("https://www.anthropic.com/research/x/", "https://anthropic.com/research/x"),
    ("http://anthropic.com/research/x", "https://anthropic.com/research/x"),
    ("https://Alignment.Anthropic.com/2025/Post/", "https://alignment.anthropic.com/2025/Post"),
    ("https://example.org/", "https://example.org"),
    # unknown parameters dropped; identity parameters kept by host, sorted
    ("https://openai.com/news/?tags=safety", "https://openai.com/news"),
    ("https://transformer-circuits.pub/2025/attribution-graphs/biology.html?featureId=123",
     "https://transformer-circuits.pub/2025/attribution-graphs/biology.html"),
    ("https://openreview.net/forum?id=AbCdEf123&noteId=XyZ", "https://openreview.net/forum?id=AbCdEf123"),
    ("https://openreview.net/pdf?id=AbCdEf123", "https://openreview.net/forum?id=AbCdEf123"),
    ("https://openreview.net/forum?id=OtherPaper", "https://openreview.net/forum?id=OtherPaper"),
    ("https://news.ycombinator.com/item?id=41000000", "https://news.ycombinator.com/item?id=41000000"),
    ("https://someblog.example/?p=1234&utm_source=rss", "https://someblog.example?p=1234"),
    ("https://someblog.example/?p=two", "https://someblog.example"),  # p not numeric: navigation
    ("https://en.wikipedia.org/w/index.php?title=AI_alignment&oldid=123&action=history",
     "https://en.wikipedia.org/w/index.php?oldid=123&title=AI_alignment"),
    # YouTube: one video, every form; playlists keep `list`
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PL1&index=2", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://youtu.be/dQw4w9WgXcQ?si=abc", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ?start=3", "https://youtube.com/watch?v=dQw4w9WgXcQ"),
    ("https://www.youtube.com/playlist?list=PLabc&si=x", "https://youtube.com/playlist?list=PLabc"),
    ("https://www.youtube.com/@AnthropicAI/videos", "https://youtube.com/@AnthropicAI/videos"),
    # Substack: subdomain post form
    ("https://open.substack.com/pub/thezvi/p/ai-150?r=4ab", "https://thezvi.substack.com/p/ai-150"),
    ("https://substack.com/@thezvi/p/ai-150", "https://thezvi.substack.com/p/ai-150"),
    ("https://thezvi.substack.com/p/ai-150/comments", "https://thezvi.substack.com/p/ai-150"),
    ("https://substack.com/home/post/p-150000000", "https://substack.com/home/post/p-150000000"),  # needs a fetch
    # Medium: the 12-hex post id
    ("https://medium.com/@someone/why-alignment-is-hard-1a2b3c4d5e6f?source=rss", "https://medium.com/p/1a2b3c4d5e6f"),
    ("https://pub.medium.com/why-alignment-is-hard-1a2b3c4d5e6f", "https://medium.com/p/1a2b3c4d5e6f"),
    ("https://towardsdatascience.com/some-title-1a2b3c4d5e6f", "https://medium.com/p/1a2b3c4d5e6f"),
    # GitHub: owner/repo case-insensitive, .git, /tree/<ref> at the root; issues and files stay keys
    ("https://github.com/OpenAI/Evals.git", "https://github.com/openai/evals"),
    ("https://github.com/openai/evals/tree/main/", "https://github.com/openai/evals"),
    ("https://github.com/openai/evals/tree/main/evals/registry", "https://github.com/openai/evals/tree/main/evals/registry"),
    ("https://github.com/openai/evals/issues/42#issuecomment-1", "https://github.com/openai/evals/issues/42"),
    ("https://github.com/openai/evals/blob/main/README.md", "https://github.com/openai/evals/blob/-/README.md"),
    ("https://raw.githubusercontent.com/QwenLM/Qwen3/refs/heads/main/README.md",
     "https://raw.githubusercontent.com/qwenlm/qwen3/-/heads/main/README.md"),
    ("https://raw.githubusercontent.com/meta-llama/llama-models/HEAD/models/llama2/MODEL_CARD.md",
     "https://raw.githubusercontent.com/meta-llama/llama-models/-/models/llama2/MODEL_CARD.md"),
    # keyed kinds in url form (a url-group row holding such a URL)
    ("https://arxiv.org/pdf/2310.01405v3.pdf", "https://arxiv.org/abs/2310.01405"),
    ("http://export.arxiv.org/abs/2310.01405v1", "https://arxiv.org/abs/2310.01405"),
    ("https://dx.doi.org/10.1145/3442188.3445922", "https://doi.org/10.1145/3442188.3445922"),
    ("https://mobile.twitter.com/sama/status/1800000000000000000/photo/1", "https://x.com/i/status/1800000000000000000"),
    ("https://twitter.com/AnthropicAI", "https://x.com/anthropicai"),
    ("https://www.greaterwrong.com/posts/AbCdEfGh123456789/some-slug", "https://lesswrong.com/posts/AbCdEfGh123456789"),
    ("https://en.m.wikipedia.org/wiki/AI_alignment", "https://en.wikipedia.org/wiki/AI_alignment"),
    # redirect wrappers and share intents (§3.4: 33 share intents each on one key: they stay navigation)
    ("https://www.google.com/url?q=https://www.example.org/a/&sa=D", "https://example.org/a"),
    ("https://twitter.com/intent/tweet?url=https://example.org/a&text=hi", "https://x.com/intent/tweet"),
    # trailing %20, archive.today wrappers, Google Docs (round 4 L4)
    ("https://asymmetricsecurity.com/newsroom/rogue-agents-investigation/%20",
     "https://asymmetricsecurity.com/newsroom/rogue-agents-investigation"),
    ("https://archive.is/o/nrZ0O/https://www.wired.com/story/browser-haunted-by-ai-agents/",
     "https://wired.com/story/browser-haunted-by-ai-agents"),
    ("https://archive.is/qSdnK", "https://archive.is/qSdnK"),
    (f"{GDOC}/d/{DOC_C}/edit?tab=t.0",
     f"{GDOC}/d/{DOC_C}"),
    (f"{GDOC}/d/{DOC_C}/export?format=txt",
     f"{GDOC}/d/{DOC_C}"),
    # Chinese hosts addressed by the query (the Chinese-coverage work): CNKI (parameter names case-insensitive), TC260, SAMR
    ("https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ202401001&uniplatform=NZKPT",
     "https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ202401001"),
    ("https://kns.cnki.net/kcms/detail/detail.aspx?QueryID=3&CurRec=1&DbCode=CJFD&DbName=CJFDLAST2024&FileName=JFYZ202401001",
     "https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ202401001"),
    ("https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ202401002",
     "https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ202401002"),
    # D752: kcms2's `v` is a session token, dropped (every kcms2 abstract link then shares the bare path's key)
    ("https://kns.cnki.net/kcms2/article/abstract?v=AbC123-xYz_&uniplatform=NZKPT&language=CHS",
     "https://kns.cnki.net/kcms2/article/abstract"),
    ("https://www.tc260.org.cn/front/postDetail.html?id=20240301164054", "https://tc260.org.cn/front/postDetail.html?id=20240301164054"),
    ("https://www.tc260.org.cn/front/bzzqyjDetail.html?id=20250124&norm_id=20241112&recode_id=57000&page=2",
     "https://tc260.org.cn/front/bzzqyjDetail.html?id=20250124&norm_id=20241112&recode_id=57000"),
    ("https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=120779016BBA61E4408DCECE3EAD92ED",
     "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=120779016BBA61E4408DCECE3EAD92ED"),
    ("https://std.samr.gov.cn/gb/search/gbDetailed?id=A1B2C3", "https://std.samr.gov.cn/gb/search/gbDetailed?id=A1B2C3"),
    # the TC260 standards library: `type` is identity (the id repeats across lists); SAMR names case-insensitive
    ("https://www.tc260.org.cn/tc260/bzzqyj/bzkdetail.shtml?id=70&type=gjbz",
     "https://tc260.org.cn/tc260/bzzqyj/bzkdetail.shtml?id=70&type=gjbz"),
    ("https://openstd.samr.gov.cn/bzgk/std/newGbInfo?HCNO=F67D3F376E0A0A0FF5317FB36B32A30A",
     "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=F67D3F376E0A0A0FF5317FB36B32A30A"),
    # not a web URL
    ("example.org/a", ""),
    ("ftp://example.org/a", ""),
    (None, ""),
]


@pytest.mark.parametrize("raw, want", KEY)
def test_url_key(raw: str | None, want: str) -> None:
    assert url_key(raw) == want
    if want:
        assert url_key(want) == want  # idempotent: a stored key re-keys to itself
        assert sr.normalize_url(raw or "") == want  # the url key kind is this rule


def test_wayback_capture_names_only_an_original_bytes_pin() -> None:
    live = "https://www.tc260.org.cn/upload/2024-09-09/1725849192841090989.pdf"
    pin = f"https://web.archive.org/web/20250708033232id_/{live}"
    assert wayback_capture(pin) == pin
    assert wayback_capture(f"HTTP://Web.Archive.org/web/20250708033232id_/{live}") == pin
    for other in (f"https://web.archive.org/web/20250708033232/{live}", f"https://web.archive.org/web/2025id_/{live}",
                  live, "", None):
        assert wayback_capture(other) is None
    assert url_key(pin) == url_key(live) == "https://tc260.org.cn/upload/2024-09-09/1725849192841090989.pdf"


def test_query_addressed_chinese_pages_keep_distinct_keys() -> None:
    """Before the Chinese-coverage work every CNKI article page and every TC260 notice keyed to its bare script path, so two
    different documents merged into one (url_key dropped their identity parameters)."""
    cnki = [f"https://kns.cnki.net/kcms/detail/detail.aspx?dbcode=CJFD&dbname=CJFDLAST2024&filename=JFYZ20240100{i}"
            for i in range(3)]
    tc260 = [f"https://www.tc260.org.cn/front/postDetail.html?id=2024030116405{i}" for i in range(3)]
    library = [f"https://www.tc260.org.cn/tc260/bzzqyj/bzkdetail.shtml?id=70&type={t}" for t in ("gjbz", "gjbzjh")]
    samr = [f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?{n}=F67D3F376E0A0A0FF5317FB36B32A30{i}"
            for n, i in (("HCNO", 0), ("hcno", 1))]
    for group in (cnki, tc260, library, samr):
        assert len({url_key(u) for u in group}) == len(group)
    # the two spellings of one SAMR record are one key
    assert len({url_key(f"https://openstd.samr.gov.cn/bzgk/std/newGbInfo?{n}=F67D") for n in ("HCNO", "hcno")}) == 1


# ---------------------------------------------------------------- link targets
LINKS = [
    # arXiv: abs/pdf/html/format, ar5iv, export, www., the vN suffix kept as an attribute
    ("https://arxiv.org/abs/2310.01405", "paper", [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")],
     "paper", {}),
    ("https://arxiv.org/pdf/2310.01405v3", "paper", [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")],
     "paper", {"arxiv_version": 3}),
    ("https://www.arxiv.org/pdf/2310.01405v2.pdf", "paper",
     [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")], "paper", {"arxiv_version": 2}),
    ("https://arxiv.org/html/2310.01405v1#S3", "paper",
     [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")], "paper", {"arxiv_version": 1}),
    ("https://ar5iv.labs.arxiv.org/html/1706.03762", "paper",
     [("doi", "10.48550/arxiv.1706.03762"), ("arxiv", "1706.03762")], "paper", {}),
    ("https://ar5iv.org/abs/1706.03762", "paper", [("doi", "10.48550/arxiv.1706.03762"), ("arxiv", "1706.03762")],
     "paper", {}),
    ("http://export.arxiv.org/abs/hep-th/9901001v2", "paper",
     [("doi", "10.48550/arxiv.hep-th/9901001"), ("arxiv", "hep-th/9901001")], "paper", {"arxiv_version": 2}),
    ("https://arxiv.org/format/2310.01405", "paper", [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")],
     "paper", {}),
    ("https://huggingface.co/papers/2310.01405", "paper",
     [("doi", "10.48550/arxiv.2310.01405"), ("arxiv", "2310.01405")], "paper", {}),
    # DOI: resolver and DOI-in-path publishers (one fixture per pattern)
    ("https://doi.org/10.1145/3442188.3445922", "paper", [("doi", "10.1145/3442188.3445922")], "paper", {}),
    ("https://dl.acm.org/doi/10.1145/3442188.3445922", "paper", [("doi", "10.1145/3442188.3445922")], "paper", {}),
    ("https://dl.acm.org/doi/pdf/10.1145/3442188.3445922", "paper", [("doi", "10.1145/3442188.3445922")], "paper", {}),
    ("https://link.springer.com/article/10.1007/s11023-020-09539-2", "paper",
     [("doi", "10.1007/s11023-020-09539-2")], "paper", {}),
    ("https://link.springer.com/chapter/10.1007/978-3-030-01234-5_6", "paper",
     [("doi", "10.1007/978-3-030-01234-5_6")], "paper", {}),
    ("https://onlinelibrary.wiley.com/doi/full/10.1111/phpr.12345", "paper", [("doi", "10.1111/phpr.12345")], "paper", {}),
    ("https://onlinelibrary.wiley.com/doi/epdf/10.1111/phpr.12345", "paper", [("doi", "10.1111/phpr.12345")], "paper", {}),
    ("https://www.tandfonline.com/doi/abs/10.1080/0020174X.2020.1234567", "paper",
     [("doi", "10.1080/0020174x.2020.1234567")], "paper", {}),
    ("https://www.science.org/doi/10.1126/science.adj1234", "paper", [("doi", "10.1126/science.adj1234")], "paper", {}),
    ("https://www.pnas.org/doi/full/10.1073/pnas.2300000120", "paper", [("doi", "10.1073/pnas.2300000120")], "paper", {}),
    ("https://www.frontiersin.org/articles/10.3389/frai.2023.123456/full", "paper",
     [("doi", "10.3389/frai.2023.123456")], "paper", {}),
    ("https://journals.plos.org/plosone/article?id=10.1371/journal.pone.0287654", "paper",
     [("doi", "10.1371/journal.pone.0287654")], "paper", {}),
    ("https://www.nature.com/articles/s41586-023-06647-8", "paper", [("doi", "10.1038/s41586-023-06647-8")], "paper", {}),
    ("https://www.nature.com/articles/s41586-023-06647-8.pdf", "paper", [("doi", "10.1038/s41586-023-06647-8")],
     "paper", {}),
    ("https://www.biorxiv.org/content/10.1101/2023.01.01.522000v2.full", "paper",
     [("doi", "10.1101/2023.01.01.522000")], "paper", {}),
    ("https://iopscience.iop.org/article/10.1088/1742-6596/1234/1/012001/meta", "paper",
     [("doi", "10.1088/1742-6596/1234/1/012001")], "paper", {}),
    ("https://www.jstor.org/stable/10.2307/2183914", "paper", [("doi", "10.2307/2183914")], "paper", {}),
    # PubMed
    ("https://pubmed.ncbi.nlm.nih.gov/31234567/", "paper", [("pmid", "31234567")], "paper", {}),
    ("https://www.ncbi.nlm.nih.gov/pubmed/31234567", "paper", [("pmid", "31234567")], "paper", {}),
    # forum posts: /posts/<id>/<slug>, sequences, ?commentId=, GreaterWrong, EA Forum, AF
    ("https://www.lesswrong.com/posts/AbCdEfGh123456789/some-slug?commentId=Xyz", "forum",
     [("fm_post", "AbCdEfGh123456789")], "forum_post", {}),
    ("https://www.lesswrong.com/s/SeqId123/p/AbCdEfGh123456789", "forum", [("fm_post", "AbCdEfGh123456789")],
     "forum_post", {}),
    ("https://www.alignmentforum.org/posts/AbCdEfGh123456789", "forum", [("fm_post", "AbCdEfGh123456789")],
     "forum_post", {}),
    ("https://www.greaterwrong.com/posts/AbCdEfGh123456789/some-slug", "forum", [("fm_post", "AbCdEfGh123456789")],
     "forum_post", {}),
    ("https://forum.effectivealtruism.org/posts/AbCdEfGh123456789/x#comments", "forum",
     [("fm_post", "AbCdEfGh123456789")], "forum_post", {}),
    # X: x.com / twitter.com / mobile / i/web/status / i/status; a profile is a url target
    ("https://x.com/sama/status/1800000000000000000", "social", [("x_post", "1800000000000000000")], "tweet", {}),
    ("https://twitter.com/sama/status/1800000000000000000?s=20", "social", [("x_post", "1800000000000000000")],
     "tweet", {}),
    ("https://mobile.twitter.com/sama/status/1800000000000000000", "social", [("x_post", "1800000000000000000")],
     "tweet", {}),
    ("https://x.com/i/web/status/1800000000000000000", "social", [("x_post", "1800000000000000000")], "tweet", {}),
    ("https://twitter.com/i/status/1800000000000000000", "social", [("x_post", "1800000000000000000")], "tweet", {}),
    ("https://x.com/AnthropicAI", "url", [("url", "https://x.com/anthropicai")], "social_profile", {}),
    # url targets and their kind hints
    ("https://openreview.net/pdf?id=AbCdEf123", "url", [("url", "https://openreview.net/forum?id=AbCdEf123")],
     "paper", {}),
    ("https://aclanthology.org/2023.acl-long.1/", "url", [("url", "https://aclanthology.org/2023.acl-long.1")],
     "paper", {}),
    ("https://github.com/anthropics/evals", "url", [("url", "https://github.com/anthropics/evals")], "repo", {}),
    ("https://youtu.be/dQw4w9WgXcQ", "url", [("url", "https://youtube.com/watch?v=dQw4w9WgXcQ")], "video", {}),
    ("https://en.wikipedia.org/wiki/AI_alignment", "url", [("url", "https://en.wikipedia.org/wiki/AI_alignment")],
     "wiki", {}),
    ("https://www.lesswrong.com/tag/ai", "url", [("url", "https://lesswrong.com/tag/ai")], "wiki", {}),
    ("https://www.nytimes.com/2025/01/01/technology/ai.html", "url",
     [("url", "https://nytimes.com/2025/01/01/technology/ai.html")], "news", {}),
    ("https://www.whitehouse.gov/briefing-room/x/", "url", [("url", "https://whitehouse.gov/briefing-room/x")],
     "gov", {}),
    ("https://www.aisi.gov.uk/blog/x", "url", [("url", "https://aisi.gov.uk/blog/x")], "other", {}),
    # shops and book stores (collect.links rejects them by rule, round 4 (L2))
    ("https://www.amazon.com/Alignment-Problem-Brian-Christian/dp/0393635821?tag=x-20", "url",
     [("url", "https://amazon.com/Alignment-Problem-Brian-Christian/dp/0393635821")], "shop", {}),
    ("https://bookshop.org/p/books/the-alignment-problem/123", "url",
     [("url", "https://bookshop.org/p/books/the-alignment-problem/123")], "shop", {}),
    ("https://twitter.com/intent/tweet?url=https://example.org", "url", [("url", "https://x.com/intent/tweet")],
     "share", {}),
    ("https://www.facebook.com/sharer/sharer.php?u=https://example.org", "url",
     [("url", "https://facebook.com/sharer/sharer.php")], "share", {}),
    ("https://cdn.openai.com/papers/some-system-card.pdf", "url",
     [("url", "https://cdn.openai.com/papers/some-system-card.pdf")], "pdf", {}),
    ("https://joecarlsmith.com/2023/01/01/some-essay/", "url",
     [("url", "https://joecarlsmith.com/2023/01/01/some-essay")], "other", {}),
    # round 4 L4: a trailing %20 no longer hides a status, a paper or a post; archive.today wrappers point at
    # their page (a bare snapshot stays a url target); a Google Doc keys on its id, fetched as plain text
    ("https://x.com/1a3orn/status/2105319756270329993%20", "social", [("x_post", "2105319756270329993")],
     "tweet", {}),
    ("https://arxiv.org/abs/2602.00986%20", "paper",
     [("doi", "10.48550/arxiv.2602.00986"), ("arxiv", "2602.00986")], "paper", {}),
    ("https://archive.is/o/lhYzZ/https://www.nytimes.com/2026/08/25/technology/irregular-ai-test-hacks.html", "url",
     [("url", "https://nytimes.com/2026/08/25/technology/irregular-ai-test-hacks.html")], "news", {}),
    ("https://archive.ph/o/AbC12/https://x.com/alice/status/1900000000000000001", "social",
     [("x_post", "1900000000000000001")], "tweet", {}),
    ("https://archive.ph/yLnGK", "url", [("url", "https://archive.ph/yLnGK")], "other", {}),
    (f"{GDOC}/d/{DOC_A}/edit?usp=sharing", "url",
     [("url", f"{GDOC}/d/{DOC_A}")], "other", {}),
    # an unknown publisher path with a DOI-looking segment is not a DOI
    ("https://example.org/10.1145/3442188.3445922", "url", [("url", "https://example.org/10.1145/3442188.3445922")],
     "other", {}),
]


@pytest.mark.parametrize("raw, group, keys, kind, attrs", LINKS)
def test_link_keys(raw: str, group: str, keys: list[tuple[str, str]], kind: str, attrs: dict[str, int]) -> None:
    t = link_keys(raw)
    assert t is not None
    assert (t.group, list(t.keys), t.kind, dict(t.attributes)) == (group, keys, kind, attrs)
    assert t.fetch_url == fetch_url(raw)
    for k, v in t.keys:  # every key is already in its kind's normal form
        assert sr.normalize_key(k, v) == {"kind": k, "value": v}
    assert set(dict(t.keys)) <= set(sr.MERGING_KEY_KINDS[group])  # keys stay within the group


def test_link_keys_relative_and_junk() -> None:
    t = link_keys("/posts/AbCdEfGh123456789/x", base="https://www.lesswrong.com/posts/other/y")
    assert t is not None and t.keys == (("fm_post", "AbCdEfGh123456789"),)
    assert link_keys("mailto:x@y.z") is None
    assert link_keys("") is None


def test_the_measured_collisions_are_split() -> None:
    """§3.4: v3's rule put 9 OpenReview papers and many YouTube videos on one key each."""
    papers = [f"https://openreview.net/forum?id=Paper{i}" for i in range(9)]
    videos = ["https://www.youtube.com/watch?v=aaaaaaaaaaa", "https://www.youtube.com/watch?v=bbbbbbbbbbb"]
    assert len({url_key(u) for u in papers}) == 9
    assert len({url_key(u) for u in videos}) == 2
