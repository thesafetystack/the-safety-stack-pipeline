"""``stack.scholarly_records`` key typing (design §6, App. A.2 ``KEY``): table-driven over
real-looking provider identifiers.

Groups: the kind vocabulary pinned against ``configs/channels.yaml`` and ``tools/check_configs.py``;
one normal form per kind (prefixes, case, versions, URL forms, junk -> no key); the arXiv–DOI bridge
and the Zenodo DOI rule in ``keys_for``; ``keys_for_source`` per channel group; ``classify_external_id``
on resolver-prefixed DOIs. v3's own key-typing cases stay in ``tests/test_fetch_metadata.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from stack import scholarly_records as sr
from stack.config import load

REPO = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO / "configs"

S2_ID = "649def34f8be52c8b66281af98ae884c09aef38b"
FM_ID = "AbCdEfGh123456789"


def test_kind_vocabulary_matches_config_and_lint() -> None:
    """Merging kinds per group == channels.yaml `channel_groups`; non-merging == the config lint."""
    import check_configs  # tools/ is on sys.path (conftest)

    groups = load(CONFIG_DIR).channels.channel_groups
    assert {g: tuple(d.merge_keys) for g, d in groups.items()} == sr.MERGING_KEY_KINDS
    assert frozenset(sr.NON_MERGING_KEY_KINDS) == check_configs.NON_MERGING_KEY_KINDS
    assert len(set(sr.KEY_KINDS)) == len(sr.KEY_KINDS)


@pytest.mark.parametrize(
    "kind, raw, expect",
    [
        # DOI: resolver prefixes stripped, lower case, URL forms percent-decoded, junk refused.
        ("doi", "10.1038/s41586-021-03819-2", "10.1038/s41586-021-03819-2"),
        ("doi", "https://doi.org/10.1145/3442188.3445922", "10.1145/3442188.3445922"),
        ("doi", "http://dx.doi.org/10.1093/MIND/LIX.236.433", "10.1093/mind/lix.236.433"),
        ("doi", "doi:10.1093/MIND/LIX.236.433", "10.1093/mind/lix.236.433"),
        ("doi", " DOI: 10.18653/v1/2020.acl-main.463 ", "10.18653/v1/2020.acl-main.463"),
        ("doi", "https://doi.org/10.1002/%28SICI%291097-4571", "10.1002/(sici)1097-4571"),
        ("doi", "https://doi.org/10.48550/arXiv.1706.03762", "10.48550/arxiv.1706.03762"),
        ("doi", "10.48550/arXiv.math.GT/0309136", "10.48550/arxiv.math/0309136"),
        ("doi", "10.5281/zenodo.20420288", "10.5281/zenodo.20420288"),
        ("doi", "not a doi", None),
        ("doi", "10.1/notzenodo", None),
        ("doi", "https://example.org/10.1145/3442188.3445922", None),
        ("doi", "", None),
        ("doi", None, None),
        # arXiv: bare id without version, lower case, old style keeps its archive prefix.
        ("arxiv", "2409.15747", "2409.15747"),
        ("arxiv", "2409.15747v2", "2409.15747"),
        ("arxiv", "arXiv:1706.03762v7", "1706.03762"),
        ("arxiv", "0704.0001", "0704.0001"),
        ("arxiv", "hep-th/9901001v1", "hep-th/9901001"),
        ("arxiv", "math.GT/0309136", "math/0309136"),
        ("arxiv", "http://arxiv.org/abs/2409.15747v2", "2409.15747"),
        ("arxiv", "https://arxiv.org/pdf/2310.06825v1.pdf", "2310.06825"),
        ("arxiv", "http://export.arxiv.org/abs/cs/0112017v1", "cs/0112017"),
        ("arxiv", "10.48550/arXiv.2409.15747", "2409.15747"),
        ("arxiv", "https://doi.org/10.48550/arXiv.2409.15747", "2409.15747"),
        ("arxiv", "10.1145/3442188.3445922", None),  # a dddd.ddddd substring of a DOI
        ("arxiv", "2409.1", None),
        ("arxiv", "https://arxiv.org/abs/2409.157471", None),
        # OpenAlex works.
        ("openalex", "W2741809807", "W2741809807"),
        ("openalex", "https://openalex.org/W2741809807", "W2741809807"),
        ("openalex", "https://api.openalex.org/works/w2741809807", "W2741809807"),
        ("openalex", "A5023888391", None),  # an author id
        ("openalex", "10.1145/3442188.3445922", None),
        # Semantic Scholar paperId.
        ("s2", S2_ID, S2_ID),
        ("s2", S2_ID.upper(), S2_ID),
        ("s2", f"https://www.semanticscholar.org/paper/Attention-is-All-you-Need/{S2_ID}", S2_ID),
        ("s2", "CorpusId:13756489", None),
        ("s2", S2_ID[:-1], None),
        # PubMed.
        ("pmid", "pmid:31452104", "31452104"),
        ("pmid", "PMID: 31452104", "31452104"),
        ("pmid", "31452104", "31452104"),
        ("pmid", "https://pubmed.ncbi.nlm.nih.gov/31452104/", "31452104"),
        ("pmid", "pmid:abc", None),
        ("pmid", "0", None),
        # Zenodo record numbers.
        ("zenodo", "10.5281/zenodo.20420288", "20420288"),
        ("zenodo", "https://doi.org/10.5281/ZENODO.7654321", "7654321"),
        ("zenodo", "https://zenodo.org/records/7654321", "7654321"),
        ("zenodo", "10.1/notzenodo", None),
        # ForumMagnum posts (case kept).
        ("fm_post", FM_ID, FM_ID),
        ("fm_post", f"https://www.lesswrong.com/posts/{FM_ID}/some-post-slug", FM_ID),
        ("fm_post", f"https://www.alignmentforum.org/posts/{FM_ID}", FM_ID),
        ("fm_post", "posts/with a space", None),
        ("fm_post", "https://example.com/posts/abc", None),
        # URLs through v3's canonical-URL rule.
        ("url", "HTTPS://Example.COM/Path/?utm_source=x#frag", "https://example.com/Path"),
        ("url", "http://example.com/a/", "https://example.com/a"),
        ("url", "example.com/a", None),
        ("url", "ftp://example.com/a", None),
        ("url", "https://", None),
        # X posts and the X context keys.
        ("x_post", "https://x.com/someone/status/1790000000000000000?s=20", "1790000000000000000"),
        ("x_post", "https://twitter.com/someone/statuses/20", "20"),
        ("x_post", "1790000000000000000", "1790000000000000000"),
        ("x_post", "@someone", None),
        ("reply_to_id", "https://x.com/a/status/123", "123"),
        ("related", "https://incidentdatabase.ai/cite/1/", "https://incidentdatabase.ai/cite/1"),
        ("relay", "not a url", None),
        # Registry ids.
        ("registry_id", "  AIID  123 ", "AIID 123"),
        ("registry_id", "   ", None),
    ],
)
def test_normal_forms(kind: str, raw: Any, expect: str | None) -> None:
    key = sr.normalize_key(kind, raw)
    assert (key["value"] if key else None) == expect
    if key is not None:
        assert key == {"kind": kind, "value": expect}


@pytest.mark.parametrize(
    "ids, expect",
    [
        # The arXiv–DOI bridge: the DataCite DOI yields both keys, and so does a bare id.
        ([("doi", "https://doi.org/10.48550/arXiv.2409.15747")],
         [("doi", "10.48550/arxiv.2409.15747"), ("arxiv", "2409.15747")]),
        ([("arxiv", "2409.15747v3")],
         [("doi", "10.48550/arxiv.2409.15747"), ("arxiv", "2409.15747")]),
        ([("arxiv", "cs/9901001")],
         [("doi", "10.48550/arxiv.cs/9901001"), ("arxiv", "cs/9901001")]),
        # Both observed: one pair each, no duplicates.
        ([("arxiv", "2409.15747"), ("doi", "10.48550/ARXIV.2409.15747v1")],
         [("doi", "10.48550/arxiv.2409.15747"), ("arxiv", "2409.15747")]),
        # A published DOI is not bridged.
        ([("doi", "10.1145/3442188.3445922")], [("doi", "10.1145/3442188.3445922")]),
        # Zenodo DOI -> doi + zenodo (v3's zenodo_id).
        ([("doi", "10.5281/zenodo.20420288")],
         [("doi", "10.5281/zenodo.20420288"), ("zenodo", "20420288")]),
        # An OpenAlex-style record: stable KEY_KINDS order, junk dropped.
        ({"pmid": "pmid:31452104", "openalex": "https://openalex.org/W2741809807",
          "doi": "https://doi.org/10.1038/S41586-021-03819-2", "s2": "junk"},
         [("doi", "10.1038/s41586-021-03819-2"), ("openalex", "W2741809807"),
          ("pmid", "31452104")]),
        ([("related", "https://b.example/x"), ("related", "https://a.example/x"),
          ("url", "https://a.example/x")],
         [("url", "https://a.example/x"), ("related", "https://a.example/x"),
          ("related", "https://b.example/x")]),
        ([("doi", "junk"), ("arxiv", ""), ("url", None)], []),
    ],
)
def test_keys_for_bridge_order_dedup(ids: Any, expect: list[tuple[str, str]]) -> None:
    got = sr.keys_for(ids)
    assert [(k["kind"], k["value"]) for k in got] == expect
    assert sr.keys_for(list(reversed(list(ids.items() if isinstance(ids, dict) else ids)))) == got


@pytest.mark.parametrize(
    "group, channel, external_id, url, expect",
    [
        ("paper", "arxiv", "2409.15747v1",  None,
         [("doi", "10.48550/arxiv.2409.15747"), ("arxiv", "2409.15747")]),
        ("paper", "openalex", "10.48550/arXiv.1706.03762", None,
         [("doi", "10.48550/arxiv.1706.03762"), ("arxiv", "1706.03762")]),
        ("paper", "openalex", "https://doi.org/10.1145/3442188.3445922", None,
         [("doi", "10.1145/3442188.3445922")]),
        ("paper", "openalex", "W2741809807", None, [("openalex", "W2741809807")]),
        ("paper", "semantic_scholar", S2_ID, None, [("s2", S2_ID)]),
        ("paper", "semantic_scholar", "pmid:31452104", None, [("pmid", "31452104")]),
        ("paper", "europepmc", "pmid:", None, []),
        ("forum", "lesswrong", FM_ID, None, [("fm_post", FM_ID)]),
        ("url", "anthropic_alignment_blog", "some-slug", "https://Alignment.Anthropic.com/2025/x/",
         [("url", "https://alignment.anthropic.com/2025/x")]),
        ("url", "manual", "https://example.com/a?b=1", None, [("url", "https://example.com/a")]),
        ("social", "x", "1790000000000000000", None, [("x_post", "1790000000000000000")]),
        ("record", "aiid", "AIID 123", None, [("registry_id", "AIID 123")]),
    ],
)
def test_keys_for_source(
    group: str, channel: str, external_id: str, url: str | None, expect: list[tuple[str, str]],
) -> None:
    got = sr.keys_for_source(group, channel, external_id, url)
    assert [(k["kind"], k["value"]) for k in got] == expect


def test_unknown_kind_and_group_fail_loudly() -> None:
    with pytest.raises(ValueError, match="unknown key kind 'arxiv_id'"):
        sr.keys_for([("arxiv_id", "2409.15747")])
    with pytest.raises(ValueError, match="unknown channel group 'papers'"):
        sr.keys_for_source("papers", "arxiv", "2409.15747")


@pytest.mark.parametrize(
    "ext, kind, arxiv_id, doi, zenodo_id",
    [
        ("https://doi.org/10.1145/3442188.3445922", "doi", "", "10.1145/3442188.3445922", ""),
        ("doi:10.5281/ZENODO.42", "doi", "", "10.5281/zenodo.42", "42"),
        ("https://doi.org/10.48550/arXiv.2409.15747", "arxiv", "2409.15747",
         "10.48550/arxiv.2409.15747", ""),
        ("arXiv:2409.15747v2", "arxiv", "2409.15747", "10.48550/arxiv.2409.15747", ""),
        ("W2741809807", "other", "", "", ""),
    ],
)
def test_classify_external_id_prefixed_forms(
    ext: str, kind: str, arxiv_id: str, doi: str, zenodo_id: str,
) -> None:
    e = sr.classify_external_id("openalex", ext)
    assert (e.kind, e.arxiv_id, e.doi, e.zenodo_id) == (kind, arxiv_id, doi, zenodo_id)
