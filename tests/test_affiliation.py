"""``annotate.affiliation`` pure core — no DB, no network, no ``Services``.

The stage is deliberately split so its logic is testable in isolation (the accuracy /
precision / recall work needs to run it on real documents without a sweep). These tests drive
``extract.py`` with stub ``llm_call``s; the stage (``main.py``) and the eval harness
(``stack/evals``) drive the same functions with a real client.

The cases that matter here are the ones that were untestable prose in v1's prompt: verbatim
copying, the superscript-legend split, "do not invent authors", and the empty-is-meaningful
answer.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any, Mapping

import pytest

from stack.canon import Canon

REPO = Path(__file__).resolve().parents[1]


def _load_extract() -> Any:
    """Import the workflow's core by PATH — workflows are not a package (``stack/stage_runner.py``
    does the same: dynamic import by path, never a package import)."""
    path = REPO / "workflows" / "annotate" / "affiliation" / "extract.py"
    spec = importlib.util.spec_from_file_location("affiliation_extract", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ex = _load_extract()


def _load_main() -> Any:
    """The stage module, by path like the stage runner loads it."""
    path = REPO / "workflows" / "annotate" / "affiliation" / "main.py"
    spec = importlib.util.spec_from_file_location("affiliation_main", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_writable(payloads: list[dict[str, Any]]) -> None:
    """The stage turns each per-author payload into one ``AFFILIATION`` item (``main._item``);
    the result must fit the registered payload type, as ``stack.annotate.select`` checks it."""
    import pyarrow as pa

    from stack.schemas.payloads.registry import payload

    items = [_load_main()._item(pl) for pl in payloads]
    pa.array([{"items": items}], type=payload("affiliation").payload_schema)


@pytest.fixture()
def canon(config: Any) -> Canon:
    return Canon.from_config(config)


def stub(payload: Mapping[str, Any] | None) -> Any:
    """An ``llm_call`` that always returns ``payload``, ignoring its inputs."""
    return lambda _authors, _doc: payload


# ─────────────────────────── name key (H-S12) ───────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Jane Doe", "jane doe"),
        ("  Jane   Doe  ", "jane doe"),
        ("José Hernández", "jose hernandez"),
        ("Ying–Dar Lin", "ying-dar lin"),   # en dash, straight from v1's corpus
        ("Yu‐Sung Wu", "yu-sung wu"),       # non-breaking hyphen, ditto
    ],
)
def test_norm_author_name(raw: str, expected: str) -> None:
    assert ex.norm_author_name(raw) == expected


@pytest.mark.parametrize(
    "meta_name,byline_name",
    [
        ("Hayes, Jamie", "Jamie Hayes"),
        ("Triantafillou, Eleni", "Eleni Triantafillou"),
        ("van der Berg, Anna", "Anna van der Berg"),
    ],
)
def test_inverted_metadata_names_match_byline_names(meta_name: str, byline_name: str) -> None:
    """Crossref/OpenAlex emit `Family, Given`; the paper prints `Given Family`. Measured on 200
    papers, failing to un-invert caused 85% of the addressable recall gap — the model read the
    affiliation correctly and alignment then discarded it (a token reordering scores below the
    0.90 fuzzy threshold, so no tier caught it)."""
    assert ex.norm_author_name(meta_name) == ex.norm_author_name(byline_name)


@pytest.mark.parametrize("name", ["King, Jr.", "Smith, III", "Chen, PhD"])
def test_suffixes_are_not_treated_as_given_names(name: str) -> None:
    """A generational/honorific suffix follows a comma but is not a given name — un-inverting it
    would produce "jr. king"."""
    assert ex.norm_author_name(name) == name.lower()


def test_un_inversion_recovers_the_affiliation_end_to_end(canon: Canon) -> None:
    """The whole point: an inverted roster name must still land its affiliation."""
    reply = {"authors": [{"author_surface": "Jamie Hayes",
                          "affiliations": [{"surface": "Google DeepMind"}]}]}
    payloads, _ = ex.extract(
        text="doc", roster=["Hayes, Jamie"], canon=canon, llm_call=stub(reply)
    )
    assert payloads[0]["affiliations"][0]["org_slugs"] == ["deepmind"]


def test_diacritic_folding_is_why_normalize_item_key_is_not_reused() -> None:
    """v3's `normalize_item_key` (NFC -> strip -> lower) keeps combining marks, so it would split
    one author into two canonical rows. This is the regression that guards the choice."""
    import unicodedata

    def normalize_item_key(s: str) -> str:  # v3 stack.types.subjects, in v3
        return unicodedata.normalize("NFC", s).strip().lower()

    assert normalize_item_key("José") != normalize_item_key("Jose")
    assert ex.norm_author_name("José") == ex.norm_author_name("Jose")


# ─────────────────────────── the header window ──────────────────────────────


def test_header_window_keeps_short_documents_whole() -> None:
    assert ex.header_window("tiny doc") == "tiny doc"


def test_header_window_takes_head_and_tail() -> None:
    """The tail exists because some venues print affiliations at the END (v1's finding)."""
    text = "H" * 9_000 + "TAIL_MARKER"
    got = ex.header_window(text, head=100, tail=20)
    assert got.startswith("H" * 100)
    assert "TAIL_MARKER" in got
    assert len(got) < len(text)


def test_header_window_strips_converter_boilerplate() -> None:
    got = ex.header_window("# Some Title\n**DOI:** 10.1/x\nJane Doe, Anthropic")
    assert got.startswith("Jane Doe")


# ──────────────────────── parsing the model's reply ─────────────────────────


def test_parse_authors_reads_the_documented_shape() -> None:
    got = ex.parse_authors(
        {"authors": [{"author_surface": "Jane Doe", "email": "j@x.com",
                      "affiliations": [{"surface": "Anthropic", "marker": "1"}]}]}
    )
    assert len(got) == 1 and got[0].author_surface == "Jane Doe"
    assert got[0].affiliations[0].surface == "Anthropic"
    assert got[0].affiliations[0].marker == "1"


@pytest.mark.parametrize("bad", [None, {}, {"authors": None}, {"authors": "nope"}])
def test_parse_authors_survives_shape_drift(bad: Any) -> None:
    """One malformed reply costs one unit's receipt, never the run."""
    assert ex.parse_authors(bad) == []


def test_parse_authors_drops_the_unknown_sentinel_and_blanks() -> None:
    """v1 wrote the literal 'unknown' for "not found"; it must not become an anchorable quote."""
    got = ex.parse_authors(
        {"authors": [{"author_surface": "Jane Doe",
                      "affiliations": [{"surface": "unknown"}, {"surface": "   "}]}]}
    )
    assert got[0].affiliations == []


def test_parse_authors_accepts_flattened_string_affiliations() -> None:
    got = ex.parse_authors({"authors": [{"name": "Jane Doe", "affiliations": ["Anthropic"]}]})
    assert got[0].affiliations[0].surface == "Anthropic"


# ─────────────────────── roster alignment (anti-invention) ──────────────────


def test_roster_is_the_truth_about_who_the_authors_are() -> None:
    """Output is always in roster order under the ROSTER's spelling, whatever the byline printed."""
    raw = ex.parse_authors(
        {"authors": [{"author_surface": "J. Doe", "affiliations": [{"surface": "Anthropic"}]}]}
    )
    out, _ = ex.align_to_roster(raw, ["Jane Doe", "Bob Roe"])
    assert [a.author_surface for a in out] == ["Jane Doe", "Bob Roe"]
    assert out[0].affiliations[0].surface == "Anthropic"   # matched on first-initial + last
    assert out[1].affiliations == []                       # genuinely absent from the reply


@pytest.mark.parametrize(
    "roster_name,byline_name,why",
    [
        ("Martin J. Chadwick", "Martin Chadwick", "roster carries a middle initial the byline drops"),
        ("Jacob Menick", "J. Menick", "byline abbreviates the given name"),
        ("Lucy Campbell-Gillingham", "Lucy Campbell-Gillingam", "byline misspells the surname"),
        ("José Hernández", "Jose Hernandez", "diacritics dropped in conversion"),
    ],
)
def test_roster_alignment_survives_real_name_drift(
    roster_name: str, byline_name: str, why: str
) -> None:
    """All four are REAL pairs measured on 200 v1 papers. Under exact-only matching each produced
    an EMPTY record, so the author read as unaffiliated rather than unmatched."""
    raw = ex.parse_authors(
        {"authors": [{"author_surface": byline_name, "affiliations": [{"surface": "Anthropic"}]}]}
    )
    out, _ = ex.align_to_roster(raw, [roster_name])
    assert out[0].affiliations and out[0].affiliations[0].surface == "Anthropic", why


def test_ambiguous_names_claim_nobody() -> None:
    """A wrong merge attaches one author's employer to another — the exact error v1 makes. When a
    loose key matches two roster entries, neither may claim it."""
    raw = ex.parse_authors(
        {"authors": [{"author_surface": "J. Smith", "affiliations": [{"surface": "Anthropic"}]}]}
    )
    out, _ = ex.align_to_roster(raw, ["Jane Smith", "John Smith"])
    assert out[0].affiliations == [] and out[1].affiliations == []


def test_roster_alignment_keeps_matches_and_drops_inventions() -> None:
    raw = ex.parse_authors(
        {"authors": [
            {"author_surface": "Jane Doe", "affiliations": [{"surface": "Anthropic"}]},
            {"author_surface": "Ghost Author", "affiliations": [{"surface": "OpenAI"}]},
        ]}
    )
    out, flags = ex.align_to_roster(raw, ["Jane Doe"])
    assert len(out) == 1 and out[0].affiliations[0].surface == "Anthropic"
    assert flags["roster_mismatch"]["returned"] == 2   # the invented author is recorded, not stored


def test_empty_roster_is_discovery_mode() -> None:
    raw = ex.parse_authors({"authors": [{"author_surface": "Jane Doe"}]})
    out, flags = ex.align_to_roster(raw, [])
    assert [a.author_surface for a in out] == ["Jane Doe"] and flags == {}


# ────────────────────────────── org resolution ──────────────────────────────


def test_text_tier_resolves_slugs_from_the_verbatim_surface(canon: Canon) -> None:
    raw = [ex.RawAuthor("Jane Doe", [ex.RawAffiliation("Anthropic", "3")])]
    got = ex.resolve(raw, canon)[0]
    assert got["author"] == "jane doe"
    assert got["resolution"] == "text"
    assert got["affiliations"][0] == {"surface": "Anthropic", "marker": "3",
                                      "org_slugs": ["anthropic"]}


def test_dual_affiliation_keeps_both_elements_separately(canon: Canon) -> None:
    """THE case the nested payload exists for: each element keeps its own surface, so a later
    anchor failure on one voids only that one."""
    raw = [ex.RawAuthor("Jane Doe", [
        ex.RawAffiliation("Massachusetts Institute of Technology", "1"),
        ex.RawAffiliation("Anthropic", "3"),
    ])]
    affs = ex.resolve(raw, canon)[0]["affiliations"]
    assert len(affs) == 2
    assert affs[0]["org_slugs"] == []            # universities are deliberately untracked
    assert affs[1]["org_slugs"] == ["anthropic"]


def test_one_surface_naming_two_orgs_yields_both(canon: Canon) -> None:
    raw = [ex.RawAuthor("Jane Doe", [ex.RawAffiliation("Google DeepMind and Redwood Research")])]
    assert ex.resolve(raw, canon)[0]["affiliations"][0]["org_slugs"] == ["deepmind", "redwood"]


def test_empty_affiliations_is_a_real_answer(canon: Canon) -> None:
    """Empty is legal and meaningful — 'no affiliation was findable' (v1's 'unknown')."""
    got = ex.resolve([ex.RawAuthor("Jane Doe")], canon)[0]
    assert got["affiliations"] == [] and got["resolution"] == "unknown"


def test_openalex_is_a_fallback_not_the_gate(canon: Canon) -> None:
    """Tier order REVERSES v1: text wins where it has an answer, OpenAlex fills the gap."""
    raw = [ex.RawAuthor("Jane Doe", [ex.RawAffiliation("Anthropic")]), ex.RawAuthor("Bob Roe")]
    oa = {"Jane Doe": ["Somewhere Else"], "Bob Roe": ["Google DeepMind"]}
    got = ex.resolve(raw, canon, openalex=oa)
    assert got[0]["resolution"] == "text"                      # OpenAlex did NOT override text
    assert got[0]["affiliations"][0]["surface"] == "Anthropic"
    assert got[1]["resolution"] == "openalex"                  # but it filled the empty one
    assert got[1]["affiliations"][0]["org_slugs"] == ["deepmind"]


def test_email_domain_is_the_last_tier(canon: Canon) -> None:
    raw = [ex.RawAuthor("Jane Doe", [], email="jane@anthropic.com")]
    got = ex.resolve(raw, canon)[0]
    assert got["resolution"] == "email_domain"
    assert got["affiliations"][0]["org_slugs"] == ["anthropic"]


def test_unrecognised_email_domain_does_not_invent_an_affiliation(canon: Canon) -> None:
    got = ex.resolve([ex.RawAuthor("Jane Doe", [], email="jane@mit.edu")], canon)[0]
    assert got["affiliations"] == [] and got["resolution"] == "unknown"


def test_byline_index_is_recorded_for_downstream_denominators(canon: Canon) -> None:
    """Lets an aggregate ask first-author-affiliated vs any-author-affiliated later."""
    raw = [ex.RawAuthor("A"), ex.RawAuthor("B"), ex.RawAuthor("C")]
    assert [p["byline_index"] for p in ex.resolve(raw, canon)] == [0, 1, 2]


# ─────────────────────────── the vision trigger ─────────────────────────────


def test_vision_triggers_on_an_empty_byline() -> None:
    assert ex.needs_vision_fallback([]) is True


def test_vision_triggers_when_most_authors_are_unresolved() -> None:
    payloads = [{"affiliations": []}, {"affiliations": []}, {"affiliations": [{"surface": "x"}]}]
    assert ex.needs_vision_fallback(payloads) is True


def test_vision_triggers_when_most_quotes_failed_to_anchor() -> None:
    """"The text the model read is not the text we stored" — a convert problem, not an
    extraction one."""
    payloads = [{"affiliations": [{"surface": "x"}]}] * 4
    assert ex.needs_vision_fallback(payloads, voided=3, quoted=4) is True
    assert ex.needs_vision_fallback(payloads, voided=0, quoted=4) is False


def test_ambiguous_assignment_fires_when_markers_were_lost() -> None:
    """The measured `arxiv.2510.13900` shape: several authors, several affiliations, no marker
    ties them — so author→org came from layout, not from the text."""
    payloads = [
        {"affiliations": [{"surface": "EPFL"}]},
        {"affiliations": [{"surface": "Anthropic"}]},
    ]
    assert ex.ambiguous_assignment(payloads) is True


def test_ambiguous_assignment_is_quiet_when_a_marker_ties_them() -> None:
    payloads = [
        {"affiliations": [{"surface": "EPFL", "marker": "1"}]},
        {"affiliations": [{"surface": "Anthropic", "marker": "2"}]},
    ]
    assert ex.ambiguous_assignment(payloads) is False


def test_ambiguous_assignment_is_quiet_on_one_shared_affiliation() -> None:
    """The common marker-less layout — every author under one org — is unambiguous, and is why
    this must not become a filter (that class measured BETTER than average)."""
    payloads = [{"affiliations": [{"surface": "Anthropic"}]}] * 4
    assert ex.ambiguous_assignment(payloads) is False


def test_ambiguous_assignment_needs_two_authors() -> None:
    assert ex.ambiguous_assignment([{"affiliations": [{"surface": "Anthropic"}]}]) is False


def test_vision_does_not_trigger_on_a_healthy_document() -> None:
    payloads = [{"affiliations": [{"surface": "Anthropic"}]}] * 3 + [{"affiliations": []}]
    assert ex.needs_vision_fallback(payloads) is False


# ──────────────────────────── end to end (stubbed) ──────────────────────────


def test_extract_end_to_end_with_a_stub(canon: Canon) -> None:
    doc = "# T\nJane Doe^1, Bob Roe^2\n1 Anthropic\n2 Google DeepMind\n\nAbstract..."
    reply = {"authors": [
        {"author_surface": "Jane Doe", "affiliations": [{"surface": "Anthropic", "marker": "1"}]},
        {"author_surface": "Bob Roe",
         "affiliations": [{"surface": "Google DeepMind", "marker": "2"}]},
    ]}
    payloads, flags = ex.extract(
        text=doc, roster=["Jane Doe", "Bob Roe"], canon=canon, llm_call=stub(reply)
    )
    assert flags == {}
    assert [p["author"] for p in payloads] == ["jane doe", "bob roe"]
    assert [p["affiliations"][0]["org_slugs"] for p in payloads] == [["anthropic"], ["deepmind"]]


def test_extract_records_discovery_failure(canon: Canon) -> None:
    payloads, flags = ex.extract(text="no byline here", roster=[], canon=canon, llm_call=stub({}))
    assert payloads == [] and flags["discovery_failed"] is True


def test_extract_output_validates_against_the_payload_model(canon: Canon) -> None:
    """The core's output must be writable as-is — this is the contract main.py relies on."""
    reply = {"authors": [{"author_surface": "Jane Doe", "email": "jane@anthropic.com",
                          "affiliations": [{"surface": "Anthropic", "marker": "1"}]}]}
    payloads, _ = ex.extract(text="doc", roster=["Jane Doe"], canon=canon, llm_call=stub(reply))
    assert payloads
    _assert_writable(payloads)


# ─────────────────────── the XML byline tier (tier-0, D-RAW1) ─────────────────


def boom(_authors: str, _doc: str) -> None:
    """An ``llm_call`` that must never run — the xml tier's whole point is skipping it."""
    raise AssertionError("llm_call invoked — the xml tier should have answered")


JATS = b"""<?xml version="1.0"?>
<article xmlns:xlink="http://www.w3.org/1999/xlink">
  <front><article-meta>
    <contrib-group>
      <contrib contrib-type="author">
        <contrib-id contrib-id-type="orcid">https://orcid.org/0000-0002-1825-0097</contrib-id>
        <name><surname>Doe</surname><given-names>Jane</given-names></name>
        <xref ref-type="aff" rid="aff1"><sup>1</sup></xref>
        <xref ref-type="aff" rid="aff2"><sup>2</sup></xref>
        <email>jane@anthropic.com</email>
      </contrib>
      <contrib contrib-type="author">
        <name><surname>Roe</surname><given-names>Bob</given-names></name>
        <xref ref-type="aff" rid="aff2"><sup>2</sup></xref>
      </contrib>
      <contrib contrib-type="editor">
        <name><surname>Editor</surname><given-names>Eve</given-names></name>
      </contrib>
    </contrib-group>
    <aff id="aff1"><label>1</label>Anthropic, San Francisco, CA</aff>
    <aff id="aff2"><label>2</label>Google DeepMind, London</aff>
  </article-meta></front>
  <body><p>Anthropic appears in the body too, and must not be re-counted.</p></body>
</article>"""

ELSEVIER = b"""<full-text-retrieval-response xmlns:ce="http://www.elsevier.com/xml/common/dtd"
    xmlns:sa="http://www.elsevier.com/xml/common/struct-aff/dtd">
  <originalText><doc><serial-item><article><head>
    <ce:author-group id="aut0001">
      <ce:author id="au0001" orcid="0000-0003-1161-4270">
        <ce:given-name>Lexin</ce:given-name><ce:surname>Zhou</ce:surname>
        <ce:cross-ref id="crf1" refid="aff0001"><ce:sup loc="post">a</ce:sup></ce:cross-ref>
      </ce:author>
      <ce:author id="au0002">
        <ce:given-name>Pablo</ce:given-name><ce:surname>Casares</ce:surname>
        <ce:cross-ref id="crf2" refid="aff0002"><ce:sup loc="post">b</ce:sup></ce:cross-ref>
      </ce:author>
      <ce:affiliation id="aff0001">
        <ce:label>a</ce:label>
        <ce:textfn>Universitat Politecnica de Valencia, Spain</ce:textfn>
      </ce:affiliation>
      <ce:affiliation id="aff0002">
        <ce:label>b</ce:label>
        <ce:textfn>FAR.ai, USA</ce:textfn>
      </ce:affiliation>
    </ce:author-group>
  </head></article></serial-item></doc></originalText>
</full-text-retrieval-response>"""


def test_jats_byline_parses_linkage_markers_email_and_orcid() -> None:
    authors = ex.parse_xml_byline(JATS)
    assert [a.author_surface for a in authors] == ["Jane Doe", "Bob Roe"]  # editor excluded
    jane, bob = authors
    assert [(f.surface, f.marker) for f in jane.affiliations] == [
        ("Anthropic, San Francisco, CA", "1"),
        ("Google DeepMind, London", "2"),
    ]
    assert jane.email == "jane@anthropic.com"
    assert jane.orcid == "0000-0002-1825-0097"  # URL prefix stripped
    assert [(f.surface, f.marker) for f in bob.affiliations] == [
        ("Google DeepMind, London", "2")
    ]


def test_jats_single_shared_aff_applies_to_every_author() -> None:
    """One <aff>, zero <xref>s: the shared-affiliation layout (prompt shape 1)."""
    xml = b"""<article><front><article-meta>
      <contrib-group>
        <contrib contrib-type="author"><name><surname>Doe</surname>
          <given-names>Jane</given-names></name></contrib>
        <contrib contrib-type="author"><name><surname>Roe</surname>
          <given-names>Bob</given-names></name></contrib>
      </contrib-group>
      <aff id="a1">Anthropic</aff>
    </article-meta></front></article>"""
    authors = ex.parse_xml_byline(xml)
    assert [[f.surface for f in a.affiliations] for a in authors] == [["Anthropic"], ["Anthropic"]]


def test_elsevier_ce_byline_parses_via_localnames() -> None:
    authors = ex.parse_xml_byline(ELSEVIER)
    assert [a.author_surface for a in authors] == ["Lexin Zhou", "Pablo Casares"]
    assert authors[0].orcid == "0000-0003-1161-4270"
    assert [(f.surface, f.marker) for f in authors[0].affiliations] == [
        ("Universitat Politecnica de Valencia, Spain", "a")
    ]
    assert [(f.surface, f.marker) for f in authors[1].affiliations] == [("FAR.ai, USA", "b")]


@pytest.mark.parametrize(
    "raw",
    [
        b"not xml at all <<<",
        b"<article><body><p>parseable, but no byline</p></body></article>",
        b"<data><row>some other xml vocabulary entirely</row></data>",
    ],
    ids=["malformed", "byline-less", "unknown-vocabulary"],
)
def test_unusable_xml_returns_none_to_fall_through(raw: bytes) -> None:
    assert ex.parse_xml_byline(raw) is None


def test_xml_tier_skips_the_llm_and_stamps_resolution(canon: Canon) -> None:
    payloads, flags = ex.extract(
        text="irrelevant", roster=["Jane Doe", "Bob Roe"], canon=canon,
        llm_call=boom, raw_xml=JATS,
    )
    assert flags["xml_byline"] is True
    assert [p["resolution"] for p in payloads] == ["xml", "xml"]
    assert payloads[0]["orcid"] == "0000-0002-1825-0097"
    assert payloads[0]["affiliations"][0]["org_slugs"] == ["anthropic"]
    assert payloads[1]["affiliations"][0]["org_slugs"] == ["deepmind"]


def test_unusable_xml_falls_through_to_the_text_tier(canon: Canon) -> None:
    reply = {"authors": [{"author_surface": "Jane Doe",
                          "affiliations": [{"surface": "Anthropic"}]}]}
    payloads, flags = ex.extract(
        text="doc", roster=["Jane Doe"], canon=canon, llm_call=stub(reply),
        raw_xml=b"<data>no byline</data>",
    )
    assert "xml_byline" not in flags
    assert payloads[0]["resolution"] == "text"


def test_xml_empty_author_still_gets_the_openalex_fallback(canon: Canon) -> None:
    """The xml tier answers per author; an author the markup left bare falls to the same
    fallback ladder the text tier uses."""
    xml = b"""<article><front><article-meta><contrib-group>
      <contrib contrib-type="author"><name><surname>Doe</surname>
        <given-names>Jane</given-names></name>
        <xref ref-type="aff" rid="a1"/></contrib>
      <contrib contrib-type="author"><name><surname>Roe</surname>
        <given-names>Bob</given-names></name></contrib>
      </contrib-group>
      <aff id="a1"><label>1</label>Anthropic</aff>
      <aff id="a2"><label>2</label>Unused Institute</aff>
    </article-meta></front></article>"""
    payloads, _ = ex.extract(
        text="irrelevant", roster=["Jane Doe", "Bob Roe"], canon=canon, llm_call=boom,
        raw_xml=xml, openalex={"Bob Roe": ["Redwood Research"]},
    )
    assert payloads[0]["resolution"] == "xml"
    assert payloads[1]["resolution"] == "openalex"
    assert payloads[1]["affiliations"][0]["org_slugs"] == ["redwood"]


def test_xml_rows_are_not_anchored_and_survive_anchoring_pass(canon: Canon) -> None:
    """anchor_payload's text-tier gate covers xml for free: surfaces from the raw markup are
    often absent from the docling markdown, and anchoring them would void good data."""
    payloads, _ = ex.extract(
        text="a converted markdown that mentions neither org", roster=["Jane Doe", "Bob Roe"],
        canon=canon, llm_call=boom, raw_xml=JATS,
    )
    class NeverAnchors:
        def anchor_detail(self, quote: str, text: str) -> Any:
            raise AssertionError("xml rows must not be anchored")
    for payload in payloads:
        out, envelope, voided = ex.anchor_payload(
            payload, NeverAnchors(), "text", "sha256:x"
        )
        assert out == payload and envelope == [] and voided == []


def test_xml_tier_output_validates_against_the_payload_model(canon: Canon) -> None:
    payloads, _ = ex.extract(
        text="", roster=["Jane Doe", "Bob Roe"], canon=canon, llm_call=boom, raw_xml=JATS
    )
    assert payloads
    _assert_writable(payloads)
