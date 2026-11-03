# Link crawling: outbound links of selected documents as candidate sources

Status: design record (revision 2, 2026-10-12), implemented since: `collect.links` (D222-D225), `collect.feeds` and
`collect.manual` (D218-D220), `collect.x-lookup` (D213, D243-D247), `stack/urls.py` and `stack migrate rekey`
(D214-D216), `stack build --passes` (D221, D226). Not implemented: page-probe, lw-lookup, `link_context`
(`workflows/collect/links/README.md`). In production since plan `batch-2026-10a` revision 5 (D658, D664):
`collect.feeds`, `collect.manual` and `collect.links`, the latter on the links of flagged manual sources only
(`rules: [flagged_links]`). Postponed: the newsletters' link harvest and X hydration (`docs/FUTURE.md`);
`configs/plans/links-scratch.yaml` is the working template. The text below is the design as proposed, with the
measurements it was based on; where it and the code differ, the code and the D-numbers win. Pipeline terms
(`sources`, `documents`, gather, triage gate, acquire, lookups, in-band entries, the pointer rule) are those of
`docs/DESIGN.md`; decision numbers `Dnnn` are `docs/DECISIONS.md`.

**The ask.** Harvest the outbound links of selected documents as candidate sources: first Zvi Mowshowitz's posts,
frontier-lab and safety-org blog posts and some LessWrong posts, later link newsletters (D198 item 1). SR 2025 got
about half of its candidate pool this way (1,654 of 3,384 rows; Zvi's posts alone 46 %; section 2). This note
proposes where link harvesting sits in the v4 pipeline, what it writes, how link targets reach triage and
acquisition, and what it costs, with numbers measured on the dev store.

## 0. Summary

**Revision 2 (2026-10-12, after the review of revision 1).** Tweets are a valid source, hydrated through the X API; linked web
pages are in the first phase; and re-running most of the pipeline after links are harvested from fetched content is
acceptable, in preference to adding copies of phases. The keyed-only first phase of revision 1 is withdrawn.

**Recommendation.**

- **`collect.links`**, a collector built like the lookups (D116). It reads the latest `documents` head, selects
  *linking documents* by source rules (`stack/rules.py`), and parses their **raw HTML** (GraphQL post bodies,
  acquired page snapshots; converted Markdown carries no links). Every run recomputes the active link map (no
  network, extraction cached) and writes only the difference: one `sources` row per target on a link channel of the
  target's identity group (`links_paper`, `links_forum`, `links_url`, `links_social`), with typed keys, a `relay`
  key per linking document, the occurrences (anchor, sentence, heading) in a record blob and a never-text
  `link_context` excerpt. Targets no longer linked are retracted.
- **Tweets** are keyed targets (`x_post`). **`collect.x-lookup`** hydrates them in batches of 100 through the X API
  (`GET /2/tweets`), writing one document per post (§5.6): text, author, date, conversation and quote keys. The URLs a
  tweet carries become link targets relayed by that tweet, which is how a Zvi-linked tweet leads to the paper it
  announces. Cost per the public docs: $5 per 1,000 tweets ($0.005 a post read), about $85-165 a year at Zvi's volume.
  Without API access the tweet stays an unresolved link-only document with its link context.
- **Web pages** get a **metadata probe** before triage: **`collect.page-probe`**, a lookup that makes one bounded GET
  per new url target and reads `<title>`, `og:*`, `article:*`, `citation_*` and the canonical link. A linked page
  then reaches triage with the kind of listing metadata a blog collector has, and follows the same triage → acquire
  → screen path, with the per-stage source rules on top (fetch by host for labs and safety orgs). Link targets
  are never rechecked weekly (a per-channel recheck setting).
- **Placement: two passes.** `stack build <plan> --passes 2` runs the build, then runs it again with the window
  collectors skipped. Pass 2's `collect.links` harvests what pass 1 acquired, and its lookups, gather, triage,
  acquire and annotate do new work only for the new rows; everything else carries. No phase is copied. The
  cross-build-only variant (`--passes 1`) remains, with one build of latency.

**Measured** (section 3): admitted targets per linking document are 9.5 on lab pages, 13.9 on LessWrong AI-tag posts
(20 for karma ≥ 50) and 16.7 on Zvi posts (n = 3); Zvi posts link about 84 distinct tweets each. In two builds on the
development store the carry stages took seconds (gather 1.7 s, triage 2.3 s, folds 0.3 s, verify about 20 s); the
4.7-minute build was LLM work on new rows.

**Volume and cost** (section 6), per week: about 800-1,500 admitted links, 300-320 tweets, 200-750 probes and
100-500 fetches. That costs under $1 for triage and screen, $1.6-3.2 for the X API, and 0.2-1.2 GB of raw blobs.
Implementation is about 34-50 agent-hours.

## 1. Need and constraints

- **Sources named:** Zvi's weekly AI posts (on LessWrong and Substack), frontier-lab and safety-org blog posts
  (already collected by `collect.blogs`, D195), some LessWrong posts; later newsletters (Gasteiger's AI Safety
  Frontier, Paleka, Lenz's paper list; D198). SR 2025 also used hand-entered link dumps (section 2); the `manual`
  channel can reuse the same link normaliser and classifier.
- **Identity once per build, on keys only** (§6): gather builds identity from `sources` after the collectors and
  lookups; acquisition never feeds identity within a build. Link targets found in content acquired in build N can
  therefore become documents in the next build at the earliest, which with two passes is minutes later (4.1).
- **Lookups are the precedent** (D116, D112): `collect.openalex-lookup` and `collect.s2-lookup` are collectors that
  read the `documents` head instead of query windows, write `sources` rows on provider channels and one `coverage`
  row per batch, and never write `documents`. At collect time the `documents` head is the previous build's.
- **Source rules** (decided earlier): per-stage rules live in one module (`stack/rules.py`) as plain code with
  configurable lists (channels, source names, URL prefixes, keywords, document types), no general config language.
  Fetching about a thousand blog documents is cheap; the reason to gate fetching is the bulk of irrelevant papers.
- **The triage gate** decides `fetch`/`skip` on the `fields` basis: title, authors, abstract and the anchor member's
  in-band excerpt (`workflows/triage/ask.py`); a row with neither abstract nor excerpt is `skipped[no_basis]` and is
  fetched once when `no_basis_fetches` is on (§3.1, D22).
- **Decided on revision 1** (2026-10-12): tweets are a valid source, to be fetched or hydrated through an API;
  linked web pages need no separate phase (they are triaged and fetched as blog pages are); re-running most of the pipeline after links are harvested from fetched content is fine, especially if the
  alternative is adding copies of existing phases.
- **Known bug in the URL key** (fixed since by `stack/urls.py`, D214-D216): `stack/collect/listing.py:norm_url`
  dropped the whole query string, so every `openreview.net/forum?id=…` collapsed to one key. Link keys hit this
  more than listing keys do (section 3.4).

## 2. What SR 2025 did (from `docs/scope/sr-2025-repo-criteria.md`)

- **Aggregator pages, chosen by hand:** 189 "collection source" pages, of which 181 were Zvi posts
  (`sources-links/zvi-posts-2025.txt`) and 8 link dumps; an LLM (`add`, Claude Haiku 4.5) typed each added URL as
  aggregator or single item, preferring single item when unsure.
- **LLM link extraction** (`collect`, Claude Sonnet 4.5, prompt `collect.yaml`): the model read the stripped page and
  returned every link that "might" be relevant with link text, a one-sentence context and a 0-1 relevancy; links at
  or above 0.3 were kept (recall-oriented; "false positives are okay"); date filter Nov 2024 onward; social media
  only for authors' own major announcements. 1,817 links from 188 pages (9.6 per page), 1,654 new classify rows.
- **Depth 1.** Extracted links went to `classify` (full-page LLM scoring); nothing was crawled further.
- **Yield.** Zvi links were 1,561 of 3,384 pool rows (46 %); 1,356 classified, **72 in the final review (5.3 %)**,
  against 30-38 % for curated sources (LW curated 84 of 276, Lenz's list 100 of 274, researcher arXiv lists 143 of
  375). The pool's 133 x.com/t.co links came mostly from Zvi posts. Whether the 72 would have been found by another
  source is not recorded; SR's other sources were narrower than our collectors.
- **Gate granularity** SR used for links: link text plus a one-sentence context, no abstract (section 6.2 of the SR
  note). That is roughly what `link_context` gives triage here.

## 3. Measurements (read-only, 2026-10-12)

**Material.** (a) A development store, its `documents` head of 2026-10-12 (1,806 documents): 180 lab-blog documents with an acquired
`text/html` raw snapshot (anthropic_alignment_blog 54, google_research 40, openai_blog 34, transformer_circuits 30,
openai_alignment_blog 22); 52 LessWrong/AF documents, 51 of them with only the `collect.forums` search body; 4 Zvi
posts; 839 text blobs. (b) Because the dev store has no LessWrong HTML bodies, a scratch store (same lineage plus
the SR collectors' windows): 97 `collect.lw_graphql` posts with the GraphQL
`htmlBody` (AI tag, 7 days; extension tags, 30 days), 3 of them by Zvi ("AI #189: New Math", "New Math from OpenAI",
"The Curve Bends You"). Nothing was written to either store. Method in Appendix A.

### 3.1 Where the links are

| Material | Links found |
|---|---|
| 839 converted texts (pymupdf4llm, trafilatura, docling) | 1 text with Markdown links (4 links); 3,919 bare URLs, 3,809 of them in the 261 PDF texts (reference lists), 68 in 188 HTML texts |
| 51 `collect.forums` LessWrong bodies (Algolia search copy, `text/markdown`) incl. 4 Zvi posts | 0 Markdown links; 15 bare URLs in total (AI #189: 94,301 characters, 0 URLs) |
| 97 `collect.lw_graphql` `htmlBody` fragments | 2,134 anchors with an http(s) target |
| 180 acquired lab pages (raw HTML) | 4,369 anchors in the main-content region (more on the full page, e.g. google_research median 125 per page vs 30 in content) |

Conversion drops anchors (trafilatura's default, docling's HTML backend), and the forum search body is a plain-text
copy. **Links must be read from raw HTML.** For LessWrong that means the GraphQL body (`collect.lw_graphql` or
`collect.af_graphql`), which the plans of the time did not run.

### 3.2 Targets per linking document, by kind

Content-region anchors, resolved against the snapshot's final URL, fragment dropped, counted once per (linking
document, target key). Kinds by URL pattern (Appendix A).

| Group | Linking docs | Content links | Unique targets per doc, mean (median) | Main kinds (share of per-doc unique targets) |
|---|---|---|---|---|
| Lab pages | 179 | 4,369 | 16.3 (13) | lab-site pages 39 % (mostly same-org navigation, figure and feature-browser pages), paper 23 %, other 15 %, repo 5 %, wiki 5 %, share buttons 3 %, PDF 3 %, forum post 2 % |
| LessWrong AI-tag posts (non-Zvi) | 87 | 1,693 | 16.4 (7) | paper 25 %, other 23 %, forum post 10 %, lab site 8 %, wiki 7 %, safety-org site 5 %, repo 5 %, PDF 5 %, forum non-post 3 %, news 2 %, Substack 2 %, tweet 2 % |
| Zvi posts | 3 | 441 | 135 (83): 282, 83, 41 | **tweet 62 %**, own Substack 21 %, other 9 %, news 3 %, lab site 2 %, paper 0.5 %, forum post 0.2 % |

Means are over linking documents with at least one link (7 of the 94 non-Zvi LessWrong posts have none; per AI-tag
post the rates are 15.2 unique and 12.8 admitted). LessWrong link density rises with karma (94 non-Zvi posts; Zvi's
three posts excluded): unique targets per post 10.1 (karma < 10, 24 posts),
14.2 (10-25, 30), 14.6 (25-50, 14), 23.5 (50-100, 15), 18.4 (≥ 100, 11); papers per post 1.9 → 5.8. Karma ≥ 50 is
26 of 94 posts (28 %). Across the 278 HTML documents there are 3,125 distinct targets outside the linking page's own
host; 302 are linked from two or more documents.

**After admission rules.** First-draft rules (drop every same-organisation link that is not a paper or forum post,
share and redirect wrappers, tweets, Zvi's self-links, wiki, news, video, podcasts, government pages, markets):

| Group | Admitted targets per doc, mean (median) | Of which paper, PDF, forum post or cross-org lab/org page | Links dropped (counts of links) |
|---|---|---|---|
| Lab pages | 7.9 (5) | 4.6 | same-org 2,023, share 198, wiki 147, government 38, video 32 |
| LessWrong (all) | 13.9 (7); karma ≥ 50: 19.4 | 8.7; karma ≥ 50: 14.5 | wiki 109, news 33, same-site 36, tweet 24 |
| Zvi | 16.7 (19) | 3.7 | tweet 264, self 89, news 14, video 8 |

The critic showed that the first-draft same-organisation drop also discarded the labs' own research pages and
PDFs (`anthropic.com/research/*`, system-card PDFs, `model-spec.openai.com`; no collector covers these hosts). With
the **revised rule** of section 4.6 (drop same-organisation navigation only; admit same-organisation PDFs and
article-like paths) lab pages admit **9.5 targets per page** (294 more same-organisation article targets), by channel
anthropic_alignment_blog 13.4, google_research 12.1, openai_alignment_blog 8.4, transformer_circuits 7.5, openai_blog
2.7. Since openai_blog is 60 % of the weekly lab flow (526 of 873 dated posts a year), the flow-weighted lab rate is
about 6 per post.

**Tweets** (per-document unique `x.com`/`twitter.com` status links): Zvi 252 over three posts (about 84 per post),
LessWrong 23 over 87 posts, lab pages 6 over 179.

**Keyed targets** (arXiv, DOI or forum-post key; revision 1's first-phase admission): lab pages 3.5 per page (442
distinct of 1,335; flow-weighted about 1.8), LessWrong 5.0 per linking post (4.6 per AI-tag post; karma ≥ 50: 7.3),
Zvi 1.0 per post (3 distinct of 47). Url-group targets are 61-94 % of what is admitted.

The "other" kind is a large share of admitted links (lab 27 %, LessWrong 27 %, Zvi 70 %): tool and product pages,
documentation, Colab notebooks, personal research sites (joecarlsmith.com, casparoesterheld.com, neelnanda.io), Google
Docs, 80,000 Hours. Triage has to separate them.

**Overlap with what the store holds.** Of the per-document unique targets, 15 % (labs), 4 % (LessWrong) and 1 % (Zvi)
resolve to a key already in the dev store. The dev store holds
1,806 documents from collector windows of days, so this says nothing about a production store; see Q1.

### 3.3 Anchor text and context

| Group | Anchors of ≥ 4 words | Generic or one-word anchors ("here", "paper", a name) | Enclosing block (p, li, blockquote, td, heading) text, median | Links without an enclosing block |
|---|---|---|---|---|
| Zvi | 48 % | 14 % | 144 characters | 0 % |
| LessWrong | 41 % | 27 % | ~310 characters | 0.5 % |
| Lab pages | 27 % | 36 % | ~300 characters | 14 % |

Zvi often anchors a whole clause on a tweet ("This included 90 of the top 500 open problems …"), so the anchor plus
its sentence usually says what the target is about. Lab pages anchor short labels; their context sentence matters
more. The GraphQL `htmlBody` is a UTF-8 fragment without a charset declaration: parsing the bytes without forcing
UTF-8 produced mojibake ("OpenAIâs") in this measurement.

### 3.4 Normalisation hazards

Over 6,503 content links:

- **Query strings left after dropping tracking parameters:** 728 links (lab 627, LessWrong 82, Zvi 19), mostly
  navigation (feature browser 191, share intents 198, `slug` 66, `tags` 55). `norm_url` maps **29 keys onto two or
  more different targets**: OpenReview `forum?id=` (9 papers on one key), YouTube `watch?v=`, openai.com `?tags=`
  index pages, share intents (twitter.com/intent, facebook sharer, linkedin shareArticle: 33 each, all navigation),
  `google.com/url?q=` redirect wrappers, transformer-circuits feature browser `?featureId=` (191 links, figure
  navigation).
- **`www.` variants:** 9 target pairs differ only by `www.`; `norm_url` keeps `www.`, so such a link and a collected
  page would not merge.
- **Shorteners:** 7 links (t.co 3, bit.ly 3, ft.trib.al 1). Not worth a network resolution in phase 1.
- **Paper links without an arXiv or DOI key:** 90 of 479 distinct paper targets on lab pages (19 %), 57 of 333 on
  LessWrong (17 %): ACM, OUP, Wiley, Springer, PLOS, nature.com, ACL Anthology, OpenReview, ICLR/ICML proceedings.
  Many carry the DOI in the path (`dl.acm.org/doi/10.1145/…`, `link.springer.com/article/10.1007/…`,
  `onlinelibrary.wiley.com/doi/…`); `normalize_doi` only reads a `doi.org` resolver URL or a bare DOI. Without a key a
  paper link lands in the `url` group and does not merge with the same paper's `paper`-group document (identity edges
  stay within a group, §6). The critic's probe: of the 147 keyless paper targets, DOI-in-path patterns recover 25 and
  `nature.com/articles/<suffix>` 45; most of the rest are PubMed and PMC links (a PMID key) and OpenReview, ACL
  Anthology and proceedings pages.

## 4. Design (recommended)

### 4.1 Placement: two passes, not copied phases

`collect.links` is a collect-phase stage like the lookups: it reads the `documents` head current at its start and
writes `sources`; gather builds identity from those rows, on keys only; acquisition never feeds identity within a
build (§6). Content acquired in one build therefore reaches identity in the next build at the earliest. Three ways
to make that next build come soon:

| | Variant | What runs | New code | Latency of a link in content acquired today |
|---|---|---|---|---|
| A | **Cross-build only** (`--passes 1`) | the plan once a week | none beyond the stages | one build (a week) |
| B | **Two passes as two builds** (`stack build <plan> --passes 2`), recommended | the build, then the same plan again with the window collectors skipped | a CLI loop and a skip rule | minutes |
| C | **Repass inside one build** (a `repass:` block in the plan) | stages up to `acquire`, then `collect.links` → lookups → gather → triage → acquire again, then the folds and annotate once | plan schema (stage names may repeat across lists; `_unique_stages` refuses it today), run loop, prior resolution for repeated stages | minutes |

**B is recommended.** It is literally "re-run most of the pipeline": `collect.links` and the lookups are ordinary
collect stages in both passes; pass 2 differs only by `--skip` of the window collectors, which have nothing new an
hour later. Pass 2 starts only when pass 1 ended `ok`. Each pass is a complete build (verify, build record), so a
failure in pass 2 leaves pass 1 as a releasable build. Priors flow as between any two builds (D112): pass 2's
triage, screen and affiliation carry every pass-1 answer and ask only about new rows; pass 2's gather carries
unchanged rows byte for byte (C6). In pass 1, `collect.links` recomputes over what the previous run already
harvested and writes nothing new.

**What pass 2 costs.** Measured on the dev store (1,806 documents, two builds of 2026-10-12): compaction
0.3 s, gather 1.7 s, triage 2.3 s (295 rows scoped, all carried), each fold 0.3 s, in-build verify about 20 s; the
second build's 4 min 41 s were screen (172 s) and affiliation (77 s) asking 50 and 20 new items. So pass 2's fixed
cost on the dev store is about 30 s plus `acquire`'s scope scan (not run in those builds; seconds without lane calls). For a
store of about 50,000 documents (an assumption) a linear extrapolation gives a few minutes for the carry stages; how
`verify` scales is not measured. A full `documents` file is about 650 bytes per document (1.18 MB for 1,806), so the
extra gather file is about 33 MB at that size. Everything else in pass 2 is work on new rows that some build has to do
anyway.

**C** saves the second verify, folds and annotate overhead and keeps one build record per run; it is the upgrade if
the per-build fixed cost grows. **A** stays available for runs where latency does not matter.

**Pass 2 skip rule.** Collect lines skipped in pass 2: every `collect.*` stage except `collect.links` and the lookups
(`*-lookup`, `collect.page-probe`); the plan can name them (`passes: {pass_2_collect: [...]}`). Stage order within the
collect phase:

```yaml
stages:
  - {stage: collect.lw_graphql, prior: [sources, coverage]}          # window collectors (pass 1 only)
  - {stage: collect.blogs, prior: [sources, coverage]}
  - {stage: collect.links, prior: [sources, coverage], options: {since: 2024-11-01, max_changed_targets: 5000}}
  - {stage: collect.x-lookup, prior: [sources, coverage], keys: [x_post], miss_cap: 3, budget: {max_usd: 5}}
  - {stage: collect.lw-lookup, prior: [sources, coverage], keys: [fm_post], miss_cap: 3}
  - {stage: collect.page-probe, prior: [sources, coverage], keys: [url], miss_cap: 2}
  - {stage: collect.openalex-lookup, prior: [sources, coverage], keys: [doi, pmid, arxiv], miss_cap: 3}
  - {stage: collect.s2-lookup, prior: [sources, coverage], keys: [arxiv, doi, pmid], miss_cap: 3}
  - {stage: gather}
  - …
```

`collect.x-lookup` runs before the other lookups because a hydrated tweet adds link targets (4.9) that the paper
lookups and the probe should see in the same pass. The lookups take the keys of this build's link rows
(`--input sources=sources.links`, `sources.x-lookup`) beside the `documents` head.

**Inputs and checks.** `collect.links` pins `--input documents=<head>` and, when the LessWrong rule reads karma, the
`sources.lw_graphql` head (D193 keeps karma in the record payload). `build.stage_inputs` (D112) gains these rows; X2
(`lineage_equality`) covers the pinned `documents` file. Every linking snapshot blob must resolve before anything is
written. With no `documents` chain yet, the stage writes nothing and ends `ok`. It makes no network requests.

### 4.2 Linking documents

| Linking source | Rule (default, `stack/rules.py` `LINKS`) | Body read |
|---|---|---|
| Zvi | a `lesswrong`/`alignmentforum` member whose author is Zvi (LessWrong user id in the rules module) | the newest in-band `post` raw `text/html` entry (`collect.lw_graphql`/`collect.af_graphql`) |
| Labs and safety orgs | a member on a channel in `LINK_SOURCE_CHANNELS` (the lab and org `collect.blogs` channels; cards later) | the pointer's current raw `text/html` snapshot (a source rule fetches every lab-blog post) |
| LessWrong, selected | karma ≥ 50 or an AF crosspost (Q2) | the newest in-band `post` raw entry |
| Tweets | a hydrated tweet relays its own URLs (4.9); it is not a linking document for `collect.links` | — |
| Newsletters (later) | a feed channel per newsletter, items minted in-band at collect (Substack feeds carry the full HTML) | the in-band entry |

A candidate is skipped when retired, masked or published before `since`. "Screen-relevant" does not select
LessWrong posts (12 of the 13 screened LessWrong documents in the dev corpus are `relevant`; AF crossposts are 3 of
94). Recorded karma is the value at first sight (median post age about 4 days), so a karma threshold is biased low.
A `via = wayback` snapshot is skipped or its `web.archive.org/web/<ts>/` prefix unwrapped.

### 4.3 Recomputation instead of a due rule

Every run: (1) the eligible linking snapshots from the pinned inputs; (2) extraction per snapshot (4.4), cached as
`cache/links/<snapshot_digest>.<extractor_digest>.json`, so only new snapshots are parsed; (3) admission and keys
under the current rules give the **active occurrences** per target; (4) per target, the row it would write (keys,
relay keys of the active linking documents, digests of the active occurrences and of the rendered `link_context`) is
compared with the latest row of its `(channel, external_id)`: new or changed → an `observed` row; no active
occurrence left → a `retracted` row (link channels: `miss_means: withdrawn`, `miss_cap: 1`, D13); unchanged →
nothing.

Edited posts, rules changes, masked, retired, merged or split linking documents and normaliser changes all take
this one path, and history is the chain's earlier rows (§5.2). There is no budget cap, so the stage always ends
`scope_complete = true` and never fails a build under `require_complete: [sources, coverage, documents]` (D22).
`max_changed_targets` (default 5,000) fails the run as a safety valve against selector or normaliser bugs; the
backfill run raises it explicitly. Coverage: one row per link channel per run, `query = {extractor_digest,
rules_digest, documents_file_id}`, `seen` = linking snapshots, `kept` = rows written.

### 4.4 Extraction

- lxml over the raw HTML; GraphQL fragments decoded as UTF-8 explicitly (no charset declaration; 3.3).
- **Region:** a GraphQL body is all content; a page uses a per-site content selector where the site has a
  `collect.blogs` spec, else the largest of all `article`, `main` and `[role=main]` elements; anchors under `nav`,
  `header`, `footer`, `aside`, `form`, `button` are dropped.
- **Per anchor:** resolve against the snapshot's `final_url`; drop the fragment and non-http schemes; unwrap redirect
  wrappers (`google.com/url?q=`, `l.facebook.com`, `out.reddit.com`, `web.archive.org/web/<ts>/`); keep the raw href.
- **Occurrence:** resolved URL, `anchor_text` (≤ 200 characters), `context` (the sentence holding the anchor in its
  enclosing block, with neighbours, up to about 400 characters), `heading` (nearest preceding h1-h4), `position`,
  linking `doc_id`, linking member `source_id`, linking canonical URL, title, channel, `published_at`, snapshot digest.
- No per-document cap (AI #189 has 308 content links). Converted text stays link-free.

### 4.5 Keys, fetch URL and normalisation

`link_keys(url) -> (group, keys, canonical_url)`, shared by `collect.links` and `collect.x-lookup`:

1. **Scholarly keys** (`stack.scholarly_records`): arXiv, DOI, DOI-in-path for publishers (ACM, Springer, Wiley,
   Taylor & Francis, Science, PNAS, PLOS `?id=`, `nature.com/articles/<suffix>`; fixtures per pattern, DOIs validated
   by `normalize_doi`), PMID from PubMed URLs → `paper`.
2. `fm_post` for LessWrong, AF, EA Forum and GreaterWrong posts (`?commentId=` maps to the post) → `forum`.
3. `x_post` for `x.com`/`twitter.com` status URLs (a status id is a strong key) → `social`.
4. Else one `url` key in a **link-safe form**: https; lower-case host without `www.`; no trailing slash; fragment and
   tracking parameters (`utm_*`, `ref`, `s`, `t`, `si`, `fbclid`, `gclid`, `mc_*`) dropped; unknown parameters
   dropped as today; identity parameters kept by a per-host allowlist (`openreview.net`: `id`; `youtube.com`: `v`,
   `list`; `news.ycombinator.com`: `id`; `journals.plos.org`: `id`; WordPress `?p=`); `raw.githubusercontent.com`
   refs folded as `norm_url` does. On the sample this form collapses 19 keys over 2,054 admitted url targets, nearly
   all navigation and redirect wrappers.
5. **`canonical_url`** (what a lane fetches) = the resolved href minus tracking parameters, not the key form.
6. **Ids:** `external_id = '<kind>:<value>'` of the primary key (C32). A normaliser change mints new sources for
   changed keys and retracts the old ones (4.3).
7. **Re-key of existing sources.** Gather does not re-type keys (`workflows/gather/normalise.py`); the new url form
   reaches `collect.blogs`, `collect.cards`, `manual` and `v3_import` rows only when they are written again. Url
   targets need a one-time re-key pass (a migration stage re-observing every url-group source) before they can merge
   with collected pages (Q6).

### 4.6 Admission

| Kind | Admitted | Channel (group) | Before triage | Fetch |
|---|---|---|---|---|
| Paper with an arXiv, DOI or PMID key | yes | `links_paper` (`paper`) | OpenAlex/S2 lookups: title, authors, date, abstract | arxiv or oa lane on `fetch` |
| Forum post (LessWrong, AF, EA Forum) | yes | `links_forum` (`forum`) | `collect.lw-lookup`: metadata and GraphQL body in-band | none needed (in-band) |
| Tweet | yes | `links_social` (`social`) | `collect.x-lookup`: text, author, date in-band (4.9) | none (in-band) |
| Web page: another organisation's page, or an article path or PDF of the linking page's own organisation; repositories (folded to the root); Substacks, personal research sites, other | yes | `links_url` (`url`) | `collect.page-probe`: title, description, date, authors, canonical link, citation keys (4.10) | http lane on `fetch` (source rules or verdict) |
| Same-organisation navigation (root, listing, tag, label, category, careers, pricing, product, feature browsers, static assets), share intents, self-links | dropped | | | |
| Wiki, news, video, podcast, government, prediction market, social profile | counted, not admitted (Q3) | | | |

Counts per kind go to the run record's `counts`.

### 4.7 Rows written by `collect.links`

- **One `sources` row per (link channel, target) and state change**, aggregated before `Collector.observe` (the
  harness keeps one observation per `source_id` per run).
- **`identity.keys`:** the target's typed keys plus one **`relay` key per active linking document** (its canonical
  URL; D13's non-merging vocabulary, the use §5.6 gives X posts' links), the 50 newest; all in the record.
- **Record:** the `provider_record` in-band entry (D522; `metadata.record_ref` until D524) = `{target, active occurrences}` (JSON in `blobs/`);
  `record_projection` covers keys, occurrence digest and `link_context` digest, so `skip_unchanged` follows every
  change.
- **`metadata`:** `title` = the earliest active occurrence's anchor when it reads as a title (≥ 4 words, ≤ 200
  characters, not a URL), else NULL; no abstract, authors or dates; `origin = provider`; `license_class` per link
  channel (`api_tos` for `links_social`).
- **`inband`:** one never-text **`link_context`** entry (`text/plain`, `blobs/text`): the three earliest active
  occurrences (`Linked from "<title>" (<channel>, <date>), section "<heading>": [<anchor>] <context>`) plus "and N
  more", stable as new links arrive. The stage fails if it cannot mint the entry (`InbandSink` disables itself
  silently after three blob errors).
- **`retracted`** when no active occurrence is left: the member gets `withdrawn_at`, its wanted roles become
  `skipped[withdrawn]`, its keys still join.

### 4.8 Provenance

`relay` keys reach `documents.identity.keys` through gather, so gate clauses, fold filters and consumers can use
them in SQL ("relayed by two or more documents": `len(list_filter(identity.keys, k -> k.kind = 'relay')) >= 2`);
for a tweet-relayed target the relay is the tweet. The occurrence records serve auditors and SR editors.

### 4.9 Tweets: `collect.x-lookup`

A tweet URL carries the post id, a strong key (`x_post`), so tweets are keyed targets like papers. In the sample they
are 62 % of Zvi's targets (about 84 distinct tweets per post), 2 % of LessWrong's and almost none of the lab pages'.
The stage follows the lookup contract of D116 and makes the `x` channel of §5.6 real (D4 left it design-only).

**API side** (public docs at docs.x.com, read 2026-10-12; **unverified** until one live call):

| Item | As documented | Use |
|---|---|---|
| Endpoint | `GET https://api.x.com/2/tweets?ids=…`, 1-100 ids per request (the docs now say "posts") | one request per 100 due keys |
| Fields | `post.fields`: `created_at, author_id, conversation_id, lang, entities, note_post, edit_controls`; `entities.urls[].expanded_url` ("the fully resolved URL"); `note_post` = full text beyond 280 characters | text, date, conversation key, links |
| Expansions | `referenced_posts` (quoted, replied-to, reposted ids and objects); `author_id` (user objects) | quotes and reposts; authors optional |
| Auth | OAuth 2.0 app bearer token (also user tokens) | `Authorization: Bearer` from the environment variable **`X_API_BEARER_TOKEN`** (name proposed; read through `STACK_ENV_FILE` as the OpenRouter key is; never logged, printed or written to a record) |
| Rate limit | `GET /2/tweets`: 3,500 requests per 15 minutes per app, 5,000 per user | not binding (a weekly run needs a handful of requests); a 429 halts the run and its keys stay due |
| Price | pay-per-use credits bought in the Developer Console: **$0.005 per post returned** (**$5 per 1,000 tweets**), $0.010 per user returned; reads deduplicated within a 24-hour UTC window; at most 3 million post reads a month; whether expanded objects in `includes` are billed is not stated | quoted posts in `includes` may double a tweet's cost; user expansion off by default (the handle comes from the status URL) |

**Rows** (channel `x`, group `social`, `license_class: api_tos`, `miss_means: withdrawn`), one document per post
(§5.6): `external_id` = the post id; keys `x_post` plus the non-merging `conversation_id`, `reply_to_id`, `quote_of`,
`repost_of`; a repost collapses to its original. `metadata`: title `@handle: <first 100 characters>`, authors
`[@handle]` (display names with the user expansion), `published_at = created_at`, `doc_type = social_post`;
The `provider_record` (D522; `record_ref` until D524) = the post object with its included referenced posts. One in-band `post` entry with both digests: the
raw JSON (`application/json`) and, minted at collect, a Markdown rendering (author, date, full text, quoted text,
expanded URLs) as its `text_digest` (C33 allows the text side), so acquire needs no JSON converter, the tweet has
text, and screen reads it. A quoted post returned in `includes` becomes its own `x` document. Errors per id (deleted,
protected, suspended) are miss rows, `retracted` after `miss_cap`. Cost is counted as posts (and users) returned ×
price in the run record; the stage line's `budget.max_usd` stops the run before it would exceed the budget, with the
rest left due. As for the existing lookups (D116), a halt or budget stop ends the run `scope_complete = false`, which
fails the build under `require_complete: [sources, …]`; so the budget is sized above the weekly volume (default $5
against $1.6-3.2), the backfill runs as operator runs outside builds, and whether lookups should count toward
`require_complete` at all is a question for the plan owner (it applies to the OpenAlex and S2 lookups today).

**Relayed URLs.** For each `expanded_url` of a hydrated post (and of a post it quotes), the stage writes a link row
through the shared link code (4.5-4.7): the same admission rules, `relay` = the tweet's URL, `link_context` =
`Posted by @handle on <date>: <text>`. A tweet that announces a paper so puts the paper (with the tweet's text as its
context) through the paper lookups and triage in the same pass. Relayed rows live in `sources.x-lookup`; when
`collect.links` also links the same target, the document has two link members, merged on the keys. Links to other
tweets become `x_post` targets that are not hydrated in the same run; depth through tweets stops there.

**Threads** are an extraction view (§5.6): v1 hydrates the linked post and what it quotes. Hydrating a whole thread
needs a search by `conversation_id` (recent search covers 7 days; older threads need full-archive search), priced
per post returned; not in v1 (Q5).

**Without API access** (variable unset, credits exhausted, access refused): the stage writes nothing for its keys,
which stay due, and the tweet remains a link-only document (`links_social`: URL, handle from the path, link
context). Triage can still judge it on Zvi's sentence, but it is never fetched (no lane for `social`). A documented
alternative is the oEmbed endpoint (`https://publish.x.com/oembed?url=…`: no authentication, "Rate limited: No" per
its page; it returns an embed blockquote with text, author and date, and t.co links that need one HEAD each to
unwrap). It is meant for embedding; whether bulk extraction through it fits X's terms is not checked (Q4).

### 4.10 Web pages: what differs from blog pages, and the metadata probe

| | Blog post found by `collect.blogs` | Page found as a link |
|---|---|---|
| Known before triage | URL, title (feed or index anchor; a slug for sitemap-only sites, D148), often the date (feed, URL path), and the site: a known lab or org blog | URL, anchor text, the sentence around it, the linking document; the host can be any site, the target a PDF, a product page, a paywall or a dead link |
| Gate | source rule: fetch every lab-blog post | to decide (below) |
| After `fetch` | http lane, conversion, screen | the same |
| Recheck | weekly for group `url` (D22) | to decide (below) |

The only difference is what is known before triage. Two ways to handle it:

1. **Triage on link context plus source rules.** No request before triage: rules fetch by host (lab and org hosts,
   PDFs on lab CDNs), the question decides the rest from anchor and sentence. Corpus rows keep no title for about
   60-75 % of url targets (only anchors of four or more words qualify) and no date, so SR's date window cannot be
   applied, and dead links and login walls are found only by fetching.
2. **A metadata probe before triage (recommended).** `collect.page-probe`, a lookup over the url keys of link-only
   url targets (this build's link rows and the `documents` head): one GET of the target's `canonical_url` through the
   http lane's transport and per-host pacing, reading at most 64 KB (stop after `</head>`), never stored as a raw
   blob. It extracts `<title>`, `og:title`, `og:description`, `og:type`, `og:site_name`, `article:published_time`,
   JSON-LD `datePublished`, `citation_title`, `citation_author`, `citation_publication_date`, `citation_doi`,
   `citation_arxiv_id`, `citation_abstract`, `<link rel=canonical>`, the HTTP status, the final URL and the content
   type. It writes a `sources` row on channel `page_probe` (group `url`): keys the url key and the canonical link's
   url key when it differs (merging tracking and `www.` variants); `title` (`og:title` > `citation_title` >
   `<title>`), `authors`, `published_at`, `doc_type`, and `abstract` = `citation_abstract`, else a description of at
   least 100 characters; the extracted fields as the `provider_record`. Errors (4xx, 5xx, timeouts) are miss rows, done after
   `miss_cap: 2`. A `citation_doi` or `citation_arxiv_id` is recorded, and the next `collect.links` recomputation
   re-keys that target as `links_paper` and retracts its url row, which resolves most keyless paper landing pages
   (OpenReview, ACL Anthology and proceedings pages carry `citation_*` meta). The probe writes `sources` from a
   network read in the collect phase, as the OpenAlex lookup does; it never writes `content`, so acquisition still
   never feeds identity.

**Why the probe.** It makes a linked page look like a collected one before triage: title, date, site and summary,
so the same source rules and the same prompt apply. It drops dead and out-of-window pages and non-content types
before triage, gives every linked page a title and date in the corpus whether or not it is fetched, and stores no
bodies of irrelevant pages. It costs one bounded request per new url target (300-800 a week, at most 50 MB of
transfer), about what fetching every linked page by rule would spend in requests, without the storage and
conversion.

**Gate rules for link-only pages** (`stack/rules.py`): fetch by rule for lab and org hosts (`LINK_FETCH_HOSTS`: the
`collect.blogs` hosts and lab CDNs) and their PDFs (system cards, papers); skip by rule when the probe found a dead
link, a login or paywall marker, a non-HTML/PDF type, or a date before `since`; the question decides the rest on
title, description and link context. A link-only document is never fetched through `no_basis`, and the plans'
`metadata.abstract IS NULL AND inband_text_digest IS NULL AND channel_group = 'url'` clause excludes the link channels.

**Recheck per target kind.** `acquire.recheck` becomes per channel (`stack/plan.py` `Recheck` has only `default`
today): `{default: weekly, links_url: never, page_probe: never}`; papers keep the arxiv lane's own version recheck;
tweets and forum posts have no fetch role (in-band). When a link target merges with a collected page, the carry rule
(4.11) gives the fetch role to the collected member and that channel's policy applies. An observed probe is never
repeated. Without this, every fetched link page would be re-requested weekly, growing to thousands of requests a
week within a year.

### 4.11 Triage, acquire and the other surfaces

**Excerpt.** `stack/checks/digests.py` ranks excerpt entries by `POINTER_RANK`, which excludes never-text roles, so
a never-text `link_context` cannot be an excerpt as the code stands. The fix is a D19/D75 amendment:
`EXCERPT_RANK = POINTER_RANK + ("link_context",)`, used only by `inband_excerpt_digest`; the pointer unchanged;
`link_context` in `CONTENT_ROLES` and `NEVER_TEXT_ROLES`. **Primary-member rule:** the anchor's best entry, except
that when it is `link_context` and another live member has a `POINTER_RANK` entry, that member's best entry is used
(the anchor never moves, so otherwise a hydrated tweet or a LessWrong body would never replace the blurb). Checks: a
`link_context` entry is never current text, never satisfies `has_text`, never reaches screen input or release text.

**What triage sees, by kind:** a paper: title, authors and abstract from the lookups; a forum post: its GraphQL
body; a tweet: its text (through the primary-member rule); a page: the probe's title, date and description, with the
link context as excerpt. Link context alone is the input only when a lookup or the probe missed. That case is
unevaluated (`safety-relevance-v3` expects a catalogue record), so before production: about 100 link-only records from
the census, labelled by their fetched targets, and a choice between the current prompt and a new prompt file with a
framing line. Labelling the excerpt "Linked from:" needs its role in the gate view (`inband_excerpt_role` beside
`inband_text_digest` in `stack/gate.py`) and in the eval freezers.

**Acquire carry** (`workflows/acquire/carry.py`): (1) a link member gets no new wanted role when a non-link live
member wants the same `content_role` or holds an in-band entry of it; (2) an unprogressed link role in that situation
becomes `skipped[not_needed]` ("covered by <source_id>"), as the one-fetch-per-identifier rule does for paper keys;
(3) when every non-link role of that content role is terminal, the link role stays due as a fallback; (4) an `ok`
link role yields the pointer to a non-link member's `ok` role and is not rechecked. Tests: a LessWrong member with an
in-band post plus a `links_forum` member; a link-only url document carried under `skip` that a blog member later
joins.

**Other surfaces:**

| Surface | Change |
|---|---|
| `stack/cli` build, `stack/build.py` | `--passes 2` (pass 2 skips the window collectors; 4.1); `stage_inputs` rows for `collect.links`, `collect.x-lookup`, `collect.page-probe`; lookups take `sources.links` and `sources.x-lookup` keys |
| `workflows/gather/merge.py` `PROVIDER_RANK` | `x` and `page_probe` after the forums, link channels after every channel (today an unlisted channel ties with pages and cards and the newest observation wins, so an anchor text would overwrite a page title) |
| new stages | `collect.links`, `collect.x-lookup`, `collect.page-probe`, `collect.lw-lookup` (`fm_post` → GraphQL record and body in-band, on `stack/collect/forummagnum.py`) |
| vocab, digests, gate view | `link_context`; `EXCERPT_RANK`; primary-member excerpt; `inband_excerpt_role` |
| `configs/channels.yaml` | `links_paper`, `links_forum`, `links_url`, `links_social`, `x` (exists, design-only), `page_probe`: groups, `miss_means`, `non_merging_keys`, `record_projection`, `license_class` |
| plans | `acquire.wants` for the link channels; per-channel `acquire.recheck`; the stage lines of 4.1 |
| release (`public-v1`) | the row rule reads the anchor member's licence; use the first non-link member's when there is one; `api_tos` rows keep, text withheld |
| URL normal form | 4.5 items 4 and 7, with the re-key migration |
| checks | V: every link row has a `relay` key and a resolvable `provider_record`; X: the `link_context` checks; the X cost counted per run |
| `stack/rules.py` | `LINKS`: linking channels and authors, kinds, hosts, per-site selectors and navigation patterns, DOI-in-path patterns, query allowlist, `LINK_FETCH_HOSTS`, probe skip rules |

### 4.12 Depth

A document whose live members are all link sources is never a linking document for `collect.links`. Tweets relay
their own URLs once (4.9), so a paper behind a tweet is reached; a tweet-relayed target never relays further. A link
target that is also a collected lab or LessWrong post is a linking document through its own channel; that is the
source rule, not depth 2.

## 5. Alternatives

| | Alternative | Verdict |
|---|---|---|
| A | **Extract inside `acquire`** and mint link sources there | rejected: acquisition would feed identity within the build, or write a chain read later, which is `collect.links` with fetch and discovery coupled |
| B | **Extract inside `gather`** | rejected: gather is pure over `sources` and the `documents` head |
| C | **LLM link selection per linking document** (SR's `collect`) | not now: cheap (a year of Zvi is at most about 5M input tokens), but non-deterministic, can invent URLs unless constrained to enumerated anchors, and duplicates triage; a phase-2 option for Zvi if triage on link context proves weak |
| D | **One source per occurrence** | rejected: one member per linking document per target; `members_digest` churn |
| E | **One `links` channel** | impossible: a channel has one identity group (D56) |
| F | **In-band collectors emit link rows themselves** (`lw_graphql`, newsletter feeds; §5.6's X pattern) | not needed with two passes; `collect.x-lookup` does it for tweets |
| G | **Keep links in converted Markdown** | rejected (3.1, 4.4) |
| H | **Route harvested keys into the existing lookups only** | rejected as the main route (no provenance, papers only); kept as the lookups reading link keys |
| I | **Keyed targets only in the first phase** (revision 1) | withdrawn: tweets have a strong key and an API; pages have the probe |
| J | **A due rule with append-only occurrences** (revision 1's first draft) | replaced by recomputation (4.3) |
| K | **Cross-build only** (variant A of 4.1) | kept as `--passes 1`; a week of latency |
| L | **Repass inside one build** (variant C of 4.1) | the upgrade if per-build fixed cost grows; needs plan schema and run-loop changes |
| M | **Fetch every linked page by rule, no probe** (option 1 of 4.10) | not the default: no titles or dates for most link pages, dead links found only by fetching, bodies of irrelevant pages stored |
| N | **Tweets through oEmbed or page scraping only** | fallback at most: no batch, t.co links to unwrap, terms not checked |

## 6. Volume and cost

**Units.** *Admitted links* = per-linking-document unique admitted targets, summed; *distinct* = after dedup across
linking documents (0.80-0.94 in the sample); *new* = distinct targets the store does not hold (unmeasured, assumed
50-80 % for url targets); *fetches* = new link-only documents that rules or triage let fetch (unmeasured share, assumed
30-50 % for pages, 20-40 % for papers). Every per-document rate is a rough census of one sample (appendix A).

**Linking documents per week:** Zvi about 3.5 (SR 2025 used 181 of his posts from its year); labs and orgs about 20
(873 dated posts in the year to 2026-10-12 across 21 blog channels, 526 of them openai_blog); LessWrong AI tag about 86
(D193), karma ≥ 50 about 25.

| Source | Docs per week | Admitted links per doc | Links per week | Tweets per doc | Tweets per week |
|---|---|---|---|---|---|
| Zvi | 3.5 | 16.7 | ~60 | 84 | ~295 |
| Labs and orgs | 20 | 6 flow-weighted (9.5 sample) | 120-190 | 0.03 | ~1 |
| LessWrong, karma ≥ 50 (default) | 25 | 20 | ~500 | 0.26 | ~7 |
| LessWrong, all AI-tag posts instead | 86 | 12.8 | ~1,100 | 0.24 | ~21 |
| Relayed by hydrated tweets (assumed 0.3-0.5 URLs a tweet) | | | 100-160 | | |
| **Total** (default / all AI-tag) | | | **~800 / ~1,500** | | **~300 / ~320** |

Of the admitted links, about 270-550 a week are paper keys (lookups), about 500-1,000 url targets (200-750 new
distinct ones to probe), the rest forum posts.

| Step | Driver | Per week (default / all AI-tag) |
|---|---|---|
| Extraction | new linking snapshots, cached | seconds |
| X API | 300-330 posts × $0.005; quoted posts in `includes` and user expansion would add up to as much again | $1.6-3.2 (about $85-165 a year) |
| Probe | 200-750 bounded GETs, ≤ 64 KB each, per-host pacing | ≤ 50 MB transfer, minutes |
| Lookups | paper keys (OpenAlex, S2), forum keys (LessWrong GraphQL) | minutes, free providers |
| Triage | 700-1,300 new rows (links and tweets) at about $0.15 per 1,000 (D203) | under $0.25 |
| Fetches | pages and papers | 100-500 |
| Storage | raw blobs: HTML mean 1.2 MB, PDF mean 3.9 MB (dev store) | 0.2-1.2 GB |
| Screen | tweets (about 300) and fetched texts, $0.4-1.0 per 1,000 (D203) | under $1 |
| Rechecks | per-channel `never` for link targets | 0 |
| Pass 2 overhead | carry stages, folds, verify | about 30 s on the dev store; a few minutes at an assumed 50,000 documents (verify not measured at scale) |

**Backfill since 2024-11-01** (about 100 weeks; staged): Zvi's 350 posts give about 29k tweets (X API about $150, up to
$300 with quoted posts) and 6k links; 1,500 lab and org posts 9-14k links; 2,500 LessWrong posts with karma ≥ 50 about
50k links; tweet-relayed 9-15k. In total about 75-85k links, 25-45k probes (hours at per-host pacing), triage about
$15, an assumed 15-35k fetches and 25-60 GB of raw blobs. Recommended order: labs, then Zvi (needs his bodies: a
`collect.lw_graphql` history window or his Substack feed), then LessWrong; the X budget per run caps the tweet part.

**Engineering:**

| Work | Agent-hours |
|---|---|
| `collect.links` (recomputation, extraction cache, `link_keys` with DOI-in-path, PMID and `x_post`, rows, rules) | 8-11 |
| `collect.x-lookup` (client, batching, rows, relayed links through the shared code, cost counting, budget, recorded-response fixtures) | 4-6 |
| `collect.page-probe` (bounded GET, head parsing, rows, re-key from citation meta) | 3-5 |
| `collect.lw-lookup` | 2-3 |
| excerpt rank, primary-member excerpt, gate-view role | 2-3 |
| acquire carry rule, per-channel recheck | 3-4 |
| URL normal form and re-key migration | 4-6 |
| `--passes 2`, gather rank, lookup and build inputs, channels, release, checks | 3-5 |
| link-context triage eval (about 100 records) | 2-3 |
| dev run (two passes) and review | 3-4 |
| **Total** | **34-50** |

The live X call needs the maintainers' X API credentials in the environment; everything else runs on recorded fixtures and the dev
store.

## 7. Phasing

1. **Phase 1, everything above:** papers, forum posts, tweets and web pages; `collect.links`, `collect.x-lookup`,
   `collect.page-probe`, `collect.lw-lookup`; the shared changes of 4.11; `--passes 2`; the URL normal form with the
   re-key migration first. Preconditions: `collect.lw_graphql` in the plans; an X API
   account with credits for the live part (without it, tweets stay link-only, 4.9). Then a two-pass dev run, which also
   gives the first marginal-yield numbers (Q1), and the link-context eval.
2. **Phase 2:** newsletter feeds with in-band HTML (and Zvi's Substack feed as a channel), PDF link annotations
   (system cards), thread hydration (Q5), LLM scoring for Zvi if link-context triage proves weak (alternative C), the
   repass-in-one-build upgrade if needed (alternative L).
3. **Phase 3:** depth 2 under rules (from screen-relevant link-derived org pages); citation and backlink walks (D198
   item 5) reusing the occurrence record.

## 8. Open questions (revision 2)

Decided on 2026-10-12 and folded in: tweets as a source hydrated through the API; web pages in the first phase;
re-running the pipeline instead of copying phases; with them the questions on Zvi's priority, the recheck policy
(now per channel, `never` for link targets) and keyless paper pages (the probe's citation meta) are settled.

- **Q1. Marginal yield:** measure it on the first two-pass dev run and, when one exists, against a production-sized
  store (default), or before building?
- **Q2. LessWrong selection:** karma ≥ 50 or AF crosspost (default; about 25 posts and 500 links a week), or every
  AI-tag post (86 posts, 1,100 links). Screen-relevant does not select; recorded karma is biased low.
- **Q3. Kinds counted but not admitted:** news (the Stack's release-news bucket may want some), video and talks,
  wiki, government documents.
- **Q4. X API account:** prepaid credits; the variable name `X_API_BEARER_TOKEN`; a per-run budget (default $5); user
  expansion for display names ($0.010 a user; default off); and whether oEmbed may serve as the fallback (its terms
  for bulk use are not checked; default: no fallback, keys stay due).
- **Q5. Threads:** hydrate the linked post and its quotes only (default), or whole threads by conversation search
  (priced per post; recent search covers 7 days).
- **Q6. URL normal form and re-key:** one normaliser for collectors and links, with a migration that re-observes every
  url-group source (a one-time identity change). Needed before url targets go live.
- **Q7. Link signal for documents that already exist:** relay keys in `sql:` rules only (default), or show link
  contexts to triage for every document (redefines `fields_digest`, D75, and re-triages the store).
- **Q8. Backfill:** since 2024-11-01, labs first, then Zvi (about 29k tweets, $150-300), then LessWrong (default)?
- **Q9. Newsletter posts themselves:** linking documents only (D198: scope (d) E7), or corpus items too?
- **Q10. Two passes:** two consecutive builds through `--passes 2` (default), a repass inside one build, or weekly
  cross-build only?

## Appendix A. Measurement method

- Scripts outside the repository (session scratch), read-only. Dev store: `stack.store.chain.read_view` over the
  `documents` chain; blobs read by digest from `blobs/raw` and `blobs/text`. Scratch store: duckdb over the
  `collect.lw_graphql` `sources` files, newest row per `source_id`; karma from the raw record payload
  (`metadata.record_ref`). Since the measurement, a concurrent shared-schema change makes the current code refuse
  the dev store's `documents` chain (`MixedSchemaError`); the critic's rerun read the two head parquet files directly
  and reproduced every number.
- **The scripts are a rough census, not the proposed extractor** (Sol): the page region is the largest element of the
  first class among `article`, `main`, `[role=main]` that has hits (not the largest across classes); anchors under
  `nav`, `header`, `footer`, `aside`, `form`, `script`, `style`, `noscript`, `button`, `select` are dropped; no
  per-site selectors, so lab numbers include some navigation (openai_blog: 244 of its 310 per-document targets are
  on its own host). The lab snapshot is the last raw `text/html` snapshot in the list, not the pointer's current one.
  The first admission script does not unwrap redirect wrappers (the table and revised-rule scripts do); one table
  counts `(key, kind)` pairs, so a key can count twice when kinds differ. Context quality is measured as the length
  of the enclosing block, not the proposed sentence and heading.
- URL: resolved against the snapshot's `final_url`; fragment dropped; `mailto:`, `javascript:`, `tel:`, `data:`
  dropped. Keys: `normalize_arxiv`, `normalize_doi`, `normalize_fm_post`, `normalize_x_post` from
  `stack.scholarly_records`, else `norm_url`. Tracking parameters for the query analysis: `utm_*`, `ref`, `fbclid`,
  `gclid`, `s`, `t`, `si`, `source`, `mc_*` and a few Substack parameters.
- Kinds: host and path lists (scholarly hosts including publishers and proceedings; lab hosts; safety-org hosts from
  `configs/channels.yaml`; news; social; repositories; video; podcasts; wiki; prediction markets; government; `.pdf`
  paths); "other" is the residual. Same organisation = same registrable domain, or one of {anthropic.com,
  transformer-circuits.pub, claude.com, claude.ai}, {openai.com, chatgpt.com}, {google.com, research.google,
  blog.google, deepmind.google, deepmind.com}. Revised rule: a same-organisation link is admitted when it is a PDF or
  has a path of at least two segments that matches no navigation pattern (root, listing, tag, label, category,
  careers, pricing, product, feature browser, static asset).
- Context = whitespace-collapsed text of the nearest `p`, `li`, `blockquote`, `td`, `dd`, `h1`-`h6` or `figcaption`
  ancestor.
- **Caveats.** Zvi: three posts from 2026-10-07 to 2026-10-09, one of them the weekly roundup AI #189. LessWrong: one
  week of the AI tag (95 of the 97 posts carry it) plus 30 days of extension tags; karma bands exclude Zvi's posts.
  Lab pages: whatever the dev windows acquired; openai_blog's are mostly product news; the safety-org blogs were not
  acquired. Overlap with the store is measured against a 1,806-document dev store only.

## Appendix B. Review record (2026-10-12)

### Revision 1: two model reviews

One critic (ran the scripts again and probed the code; every headline number reproduced except the storage
line) and one review by a second model ("Sol", gpt-6-sol, on a packet of the proposal, design and decision excerpts, code excerpts and
the measurement outputs). Both accept the placement: a lookup-like collector over the previous build's `documents`
head, one row per target, relay provenance. What changed:

| Finding | Reviewer | Change |
|---|---|---|
| `link_context` cannot be both excerpt and never text: the excerpt rank is the pointer rank | critic, blocking (run) | separate `EXCERPT_RANK`, D19/D75 amendment, fail if the context cannot be minted (4.7, 4.9) |
| A capped snapshot is marked done with links unread; caps fail builds under `require_complete` | Sol, blocking; critic, major | recomputation without budget caps; a failing safety valve (4.3) |
| Append-only occurrences keep removed or newly inadmissible links active | Sol, blocking | active occurrences recomputed each run; `retracted` when none is left (4.3, 4.7) |
| The harness takes one observation per source per run; the projection must cover every change | Sol, blocking | aggregate before observe; `record_projection` covers keys, occurrences and context (4.7) |
| The carry rule leaves double fetches and double rechecks | critic, major (run); Sol | four-part carry rule with fallback, tests (4.9) |
| The same-organisation drop discards the labs' research pages and PDFs | critic, major (run) | navigation-only drop; numbers re-measured (3.2, 4.6) |
| Url targets have no route to title, date or authors; weekly rechecks uncosted | critic, major | url targets moved to phase 1b with a page-metadata lookup and a per-channel recheck (4.6, 4.9, 6) |
| "Screen-relevant" does not select LessWrong posts | critic, major (run) | karma ≥ 50 or AF as the default; Q3 reframed (4.2, 8) |
| Triage on link context is unevaluated; the label needs the excerpt's role | critic, major | phase 1 targets reach triage with abstracts or bodies; eval before phase 1b; `inband_excerpt_role` (4.9) |
| URL migration: gather does not re-type keys; fetch URL versus key; old ids | Sol, critic | re-key migration, `canonical_url` as fetch URL, ids through recomputation (4.5) |
| Units, karma bands, lab flow weighting, backfill and storage arithmetic | Sol, critic | units defined, flow-weighted rates, corrected totals (3.2, 6) |
| Anchor-based excerpt and release licence; context churn; PMID; `origin = link` | critic, minor | primary-member excerpt and release row rule; earliest three occurrences; PMID key; `origin` unchanged (4.5, 4.7, 4.9) |
| Cross-build input checks; first build; wayback snapshots | Sol, critic | X2 plus a corpus-lineage check; empty first run; wayback unwrap or skip (4.1, 4.2) |
| Missing alternatives: keyed-only v1; in-band collectors emitting links | critic | I adopted as phase 1; F reframed (5) |

### Revision 2 (decisions of 2026-10-12)

| Point decided | Change |
|---|---|
| Tweets are a valid source, fetched or hydrated through an API | tweets admitted as keyed targets; `collect.x-lookup` on `GET /2/tweets` with documented limits and prices; relayed URLs; fallback (4.9) |
| Linked web pages are triaged and fetched as the HTML pages collected already | the difference is what is known before triage; `collect.page-probe` supplies it; source rules and per-channel recheck (4.10) |
| Re-running most of the pipeline is acceptable, in preference to copies of phases | two passes as two builds (`--passes 2`), cross-build and repass variants compared with measured carry costs (4.1) |
| keyed-only first phase, phase 0 before building | withdrawn; phase 1 covers papers, forum posts, tweets and pages; marginal yield measured on the first two-pass run (Q1) |
