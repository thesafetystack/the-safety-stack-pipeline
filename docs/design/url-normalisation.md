# URL normalisation: one module, its rules, and the re-key migration

Status: implemented (2026-10-12; D214-D216, fixes in D233). Terms are those of `docs/DESIGN.md`; the
proposal this implements is `docs/design/link-crawling.md` §3.4 (hazards), §4.5 (keys, fetch URL, re-key) and §4.6
(kinds). Code: `stack/urls.py`; tests: `tests/test_urls.py` (table-driven, real URL shapes) and
`tests/test_migrate.py::test_rekey_predicts_merges_and_splits_and_gather_agrees`.

## 1. What existed (inventory, before this change)

Every place in `stack/`, `workflows/`, `tools/` that normalised a URL or chose a URL-like identity:

| Site | Rule | Callers / where it lands |
|---|---|---|
| `stack/collect/listing.py:norm_url` (v3's rule) | https forced; whole netloc lower-cased (port, credentials kept); **`www.` kept**; **query and fragment dropped**; trailing slash dropped; `raw.githubusercontent.com` ref → `-` | `scholarly_records.normalize_url` (every `url`, `related`, `relay` key); `collect.blogs` canonical_url |
| `stack/scholarly_records.py` `normalize_doi` | resolver prefixes (`doi.org`, `dx.doi.org`, `doi:`) stripped, lower case, `10.NNNN/…`; arXiv DataCite DOI rewritten | every DOI key; no DOI-in-path URLs |
| `normalize_arxiv` | `arxiv.org/(abs|pdf|html)/<id>[vN][.pdf]` searched anywhere, version dropped; bare/`arXiv:` ids; the arXiv DOI | arXiv keys; no `format`, ar5iv-org or mirror forms |
| `normalize_fm_post` | `(www.)?(lesswrong.com|alignmentforum.org|greaterwrong.com|forum.effectivealtruism.org)/posts/<id>` | forum keys; no `/s/<seq>/p/<id>` |
| `normalize_x_post` | `(www.|mobile.)?(x|twitter).com/<user>/status(es)/<id>` | `x_post` and the non-merging X kinds; no `/i/web/status/` |
| `normalize_pmid` | `pmid:N`, bare digits, `pubmed.ncbi.nlm.nih.gov/N` | paper keys |
| `keys_for_source` | url group: `url` key from `canonical_url` (else the external id) | blogs, cards, v3_import |
| `collect.blogs` | canonical_url = `norm_url(listing link)` (key form) | the acquire http lane fetches it |
| `collect.cards` | canonical_url = the link **as listed** (`www.`, trailing slash, query kept); key = `norm_url` | the two differ for the same row |
| `collect.forums`, `lw_graphql`, `af_graphql` | canonical_url = `<base>/posts/<_id>` (`https://www.lesswrong.com`, `https://www.alignmentforum.org`) | key `fm_post` only |
| `collect.arxiv_sweep` | `https://arxiv.org/abs/<id>` (versionless) | |
| `collect.openalex` / `semantic_scholar` / lookups | arXiv abs URL, else `https://doi.org/<doi>`, else the provider's landing page **raw** (OpenAlex) or `www.semanticscholar.org/paper/<id>` | paper group |
| `collect.v3_import` | v3's canonical_url copied | key via `keys_for_source` |
| `stack/lookup/card_date_backfill.py` | key = the member's `key_value` (or canonical_url), written as the `url` key **untyped** | lookup rows |
| `workflows/gather` | never re-types keys; `_member_key` falls back to the raw canonical_url | identity block |
| acquire http lane (`choose_url`) | fetches `canonical_url` as stored; snapshot keeps `final_url` (not fed back) | |
| `acquire/lanes/oa.py` | DOI from the doi key, else a `doi.org/` prefix stripped from canonical_url (no unquote, no lower case) | |
| `stack/oa_adapter.py` cache key; `stack/http.py` robots key | host lower-cased + unquoted path + sorted query; robots by raw netloc (not lower-cased) | caches only |
| `stack/arxiv.py` | unanchored id search, version dropped; builds `arxiv.org/pdf/<id>v<N>` | arXiv lane fetch URLs |
| `stack/evals/sr.py` | host without `www.` (classification only); OpenReview `?id=`; DOI regex in the URL; `normalize_url` for the rest | SR freezer key matching |
| `stack/rules.py` | `url_prefixes` matched `canonical_url` with `starts_with`, assuming `norm_url` form | triage gate |

**Where they diverged.** (1) `canonical_url` was the key form for blogs and the listed form for cards, so
`rules.py`'s assumption held for one collector only. (2) `www.` was kept by every key: a link to `anthropic.com/x` and
a collected `www.anthropic.com/x` did not merge (9 pairs in the link sample, §3.4). (3) Every query parameter was
dropped from keys: 29 keys in the link sample collapsed different targets (OpenReview `forum?id=`, YouTube `watch?v=`,
share intents, `google.com/url?q=`). (4) No DOI-in-path, PubMed-path, `/s/<seq>/p/`, `/i/web/status/` or ar5iv forms:
such links stayed url targets and could not merge with the paper, forum or social group. (5) The arXiv version is
dropped everywhere in identity; it lives in the arXiv lane's snapshot `revision` and the lookups' `arxiv_version`
payload field (`metadata.provider_version` is the record *shape* version, not a paper version). (6) The card-date
backfill wrote its `url` key untyped. (7) Caches (`oa_adapter`, `http` robots) key on their own forms; they are not
identity and are left as they are.

## 2. The module: `stack/urls.py`

Pure, standard library only, no network. Hosts, parameters and patterns are module constants; a "host" entry covers
its subdomains.

| Entry point | Answers | Returns |
|---|---|---|
| `fetch_url(raw, base=None)` | what a lane fetches | `str` or `None` (not http(s)) |
| `url_key(raw)` | the `url` merging key | `str`, `''` when not an absolute http(s) URL |
| `link_keys(raw, base=None)` | what a link points at | `LinkTarget(group, keys, fetch_url, kind, attributes)` or `None` |
| `arxiv_of_url`, `doi_of_url`, `pmid_of_url`, `fm_post_of_url`, `x_status_of_url`, `x_profile_of_url` | raw ids from URL forms | used by `scholarly_records.normalize_*` |

**Fetch form** (`fetch_url`):

| Rule | Detail |
|---|---|
| resolve | literal and encoded whitespace (`%20`, `%09`, `%0A`, `%0D`, `%C2%A0`) stripped from both ends (paradigm3's hrefs end in `%20`, which kept 13 X statuses on `url` keys); `urljoin(base, raw)`; tabs/newlines removed, inner spaces `%20`; `//host` → https; http(s) only |
| unwrap redirect wrappers (up to 4 deep) | `google.<tld>/url?q=|url=`, `l.facebook.com`/`lm.`/`m.facebook.com/l.php?u=`, `l.instagram.com?u=`, `out.reddit.com?url=`, `youtube.com/redirect?q=`, `slack-redir.net/link?url=`, `t.umblr.com/redirect?z=`, `linkedin.com/redir/redirect?url=`, `*.safelinks.protection.outlook.com?url=`, `duckduckgo.com/l/?uddg=`, `steamcommunity.com/linkfilter/?url=`; `web.archive.org/web/<ts>[flags]/<url>` → the archived URL; archive.today `archive.(is|ph|today|li|vn|md)/o/<id>/<url>` → `<url>` (a bare snapshot `archive.ph/<id>` is the archived copy and stays a url target) |
| Google Docs | `docs.google.com/document[/u/<n>]/d/<id>/…` → `https://docs.google.com/document/d/<id>/export?format=txt`: the http lane takes the `text/plain` body (stamped `application/octet-stream`, `stack/mediatype.py`) and convert passes it through as text; the editor page is UI chrome. Published docs (`/document/d/e/<id>/pub`, HTML) and Forms are left as they are |
| tracking dropped everywhere | `utm_*`, `pk_*`, `mtm_*`, `hsa_*`; `fbclid gclid gclsrc dclid gbraid wbraid msclkid yclid twclid ttclid li_fat_id igshid igsh mc_cid mc_eid _hsenc _hsmi __hstc __hssc __hsfp hsCtaTracking mkt_tok oly_anon_id oly_enc_id vero_id vero_conv rb_clickid s_cid ref ref_src ref_url triedRedirect isFreemail showWelcome cmpid wt.mc_id spm _ga _gl` |
| tracking only on these hosts | X/Twitter `s t ref_src ref_url src`; YouTube `si feature pp ab_channel`; Spotify `si nd context`; Instagram; LinkedIn `trk trackingId lipi …`; Medium `source gi`; Reddit `share_id rdt context`; Facebook `mibextid rdid sfnsn`; TikTok; Substack `r s publication_id post_id sd token action` (also on any host's `/p/<slug>` path: custom-domain Substacks) |
| kept | every other parameter, in its original encoding; the path as given (trailing slash, case); scheme and host lower-cased; default port and credentials dropped; fragment dropped |

**Key form** (`url_key`) = the fetch form, then:

| Rule | Detail |
|---|---|
| scheme, host | `https`; host without `www.`; `m.`/`mobile.` dropped on YouTube, Twitter/X, Facebook, Reddit, Wikipedia (`<lang>.m.wikipedia.org`), IMDb; mirrors folded: `twitter.com`, `fxtwitter`, `vxtwitter`, `fixupx`, `nitter.net` → `x.com`; `old.`/`np.`/`new.reddit.com` → `reddit.com`; `greaterwrong.com` → `lesswrong.com`, `ea.greaterwrong.com` → `forum.effectivealtruism.org`; `youtube-nocookie.com` → `youtube.com`; `export.arxiv.org`, ar5iv → `arxiv.org`; `dx.doi.org` → `doi.org` |
| path | trailing slash dropped; case kept (except GitHub owner/repo) |
| query | only identity parameters, sorted: OpenReview `id`; YouTube `v` (`list` on `/playlist` only); Hacker News `id`; PLOS `id`; SSRN `abstract_id`; Apple Podcasts `i`; Google Play `id`; Google Drive `id`; Facebook `story_fbid id fbid v`; Wikipedia `title oldid curid`; IEEE `arnumber`; ACM `id`; CiteSeerX `doi`; WeChat articles `__biz mid idx sn`; NeurIPS `paper_id`; CNKI `dbcode dbname filename` (names case-insensitive, kept lower case; kcms2's session token `v` dropped, D752); TC260 `id norm_id recode_id`; the SAMR standards portals `hcno` (openstd) and `id` (std); on any host a numeric WordPress `p` / `page_id` |
| arXiv | `arxiv.org/(abs|pdf|html|format|ps)/<id>[vN][.pdf]`, ar5iv, `huggingface.co/papers/<id>`, `alphaxiv.org/(abs|overview|pdf)/<id>` → `https://arxiv.org/abs/<id>` |
| DOI resolver | `https://doi.org/<normal DOI>` |
| X | status (`/<user>/status/<id>`, `/i/web/status/<id>`, `/i/status/<id>`) → `https://x.com/i/status/<id>`; profile → `https://x.com/<handle lower-cased>` |
| YouTube | `youtu.be/<id>`, `/shorts/ /embed/ /live/ /v/ /e/<id>`, `watch?v=` → `https://youtube.com/watch?v=<id>` |
| OpenReview | `/forum|/pdf|/attachment?id=X` → `https://openreview.net/forum?id=X` |
| Substack | `open.substack.com/pub/<pub>/p/<slug>`, `substack.com/@<pub>/p/<slug>`, `<pub>.substack.com/p/<slug>/…` → `https://<pub>.substack.com/p/<slug>`; `substack.com/home/post/p-<n>` kept (needs a fetch) |
| Medium | `medium.com`, `*.medium.com`, `towardsdatascience.com` paths ending `-<12 hex>` → `https://medium.com/p/<hex>` |
| GitHub | owner/repo lower case, `.git` dropped, `/tree/<ref>` at the root → the repo; `/blob/<ref>/<path>` → `/blob/-/<path>` (one file path, one key); issues, pulls, discussions and deeper `/tree/` paths kept |
| `raw.githubusercontent.com` | owner/repo lower case, the ref → `-` (as `norm_url` did) |
| forum post URLs | `https://<forum host>/posts/<id>` |
| Google Docs | `https://docs.google.com/document/d/<id>` (the export URL and every editor URL of a doc) |

**Link targets** (`link_keys`): arXiv (above, attribute `arxiv_version`) → `paper` with `arxiv` + `doi` (the bridge);
`doi.org`/`dx.doi.org` and DOI-in-path publishers → `paper` with `doi` (validated by `normalize_doi`): the generic
Atypon form `/doi/[abs|full|pdf|epdf|pdfdirect|fullHtml|reader|book|epub|figure|suppl/]10.…` on any host (ACM, Wiley,
Taylor & Francis, Science, PNAS, SAGE, ACS, …), Springer `article|chapter|book|referenceworkentry|protocol|content/pdf`,
Frontiers `articles/<doi>[/full]`, IOP, APS, bioRxiv/medRxiv (`vN` and `.full` dropped), JSTOR `stable/10.…`, PLOS
`?id=`, `nature.com/articles/<suffix>` → `10.1038/<suffix>`; PubMed (`pubmed.ncbi.nlm.nih.gov/<n>`,
`ncbi.nlm.nih.gov/pubmed/<n>`) → `pmid`; LessWrong / AF / GreaterWrong / EA Forum `/posts/<id>[/slug]` and
`/s/<seq>/p/<id>` (`?commentId=` and fragments point at the post) → `forum` + `fm_post`; X status forms → `social` +
`x_post` (also behind a trailing `%20` and an archive.today `/o/` wrapper); everything else → `url`
with one `url` key. Kind hints for url targets, first match wins: `share` (intents
of X, Facebook, LinkedIn, Reddit submit, HN submitlink, Telegram, Pinterest, Bluesky, WhatsApp, Threads; added beside
§4.6's list because admission drops them), `social_profile`, `repo` (GitHub, GitLab, Bitbucket, Codeberg, Hugging
Face, gists, raw GitHub, sourcehut), `video`, `wiki` (Wikipedia and sister projects, Arbital, AISafety.info, forum
`/tag/ /w/ /wiki/ /topics/` paths), `news` (a list of ~55 outlets), `gov` (`.gov`, `.gov.uk`, `.europa.eu`, …;
`aisi.gov.uk` and `nist.gov` are `other`: research organisations), `paper` (OpenReview, ACL Anthology, PMLR,
NeurIPS/NIPS proceedings, AAAI, CVF, IEEE, Semantic Scholar `/paper/`, SSRN, ResearchGate `/publication/`, Europe PMC,
PMC, JMLR, OUP, ScienceDirect, Cambridge, MDPI, PhilPapers, OSF preprints, Zenodo, …), `pdf` (path ends `.pdf`),
`other`.

**Key typing.** `scholarly_records.normalize_url` (and with it the `related`/`relay` kinds) is `url_key`;
`normalize_arxiv`, `normalize_doi`, `normalize_pmid`, `normalize_fm_post`, `normalize_x_post` accept the URL forms
above and stay the key-typing entry points. v3's `listing.norm_url` is retired (its tests now pin `url_key`).

**`identity.canonical_url` is the fetch form** (decision). For url-group rows it holds `fetch_url` of the link as the
collector saw it (`www.`, trailing slash and content parameters kept); keys hold the key form; nothing compares
`canonical_url` for identity or rules. Reasons: the key form is not always fetchable (`www.`-only hosts, dropped
unknown parameters, Medium/YouTube rewrites), and only the fetch form keeps the original link a later rule change can
re-key from (the record payload is not stored for listing collectors). Consequences: `collect.blogs` now stores the
listed link (it stored the key form), `collect.cards` and `collect.v3_import` its fetch form; `stack/rules.py`
`url_prefixes` match the document's `url` keys (`identity.keys`) and are written in key form (`https://anthropic.com/`
for `www.anthropic.com`, `raw.githubusercontent.com/qwenlm/`), which changes the compiled SQL of the
`frontier_lab_posts` rule and so the gate digests of dev-v2-m4 and std-v2 (the next triage re-selects, no LLM calls).
A blog post whose fetch form differs from its stored record (a trailing slash, a kept query) writes one changed row on
its next collect (`skip_unchanged` compares the record digest), once.

**arXiv version.** `link_keys` returns it as `attributes["arxiv_version"]` for `collect.links`' rows to keep in their record.
No `sources` column holds a paper version and none was added: `metadata.provider_version` is the record shape
version (`fetch.metadata` `PROVIDER_VERSION`, v3's workflow version), and `arxiv_sweep` leaves it null; the version
already lives in the lookups' `arxiv_version` payload field and the arXiv lane's snapshot `revision`.

## 3. The re-key migration

**Mechanics.** `stack migrate rekey [--apply] [--report FILE]` (`stack/migrate.py`), a sibling of `stack migrate
upcast`. For every `sources` chain, the view's row per `source_id` gets new `keys` and `canonical_url`: url group from
the best original URL (the record payload via the `provider_record` in-band entry, D524, when it holds a link; else `canonical_url`; else a
URL `external_id`), other groups by re-normalising each stored key (a value that no longer normalises is kept, and
counted; none were). Rows that change are written as one part file per chain on its head, one recorded run per chain
(stage and writer `migrate.rekey`; a file id is `<run_id>.<family>`, so a run writes one `sources` file), header
`inputs = {sources: <head>}`, `config_digest` = `H` of the sources of `stack/urls.py` and `stack/scholarly_records.py`;
`source_id`, `external_id`, `observed_at`, `record_digest`, metadata and in-band entries are kept, `run_id` is the
migration's. Gather's observation stats now count distinct `observed_at` per source, so a restated row is not a new
observation (no store row shared an `observed_at` before). The dry run predicts the documents a gather would make
with gather's own pure functions (observations, overrides, edges, components, anchors, continuity) over the re-keyed
views in memory, with the `documents` head read as its upcast, and diffs them with the head: merges (several live
doc ids now one document; titles and the keys that join them), splits (one live document now several; the head's keys
and each part's keys). The same prediction over the unchanged views is the baseline and must reproduce the head.

Applied on 2026-10-12 to the development stores of the time: the prediction matched the gather that followed, and
there were no merges or splits, only `url` keys losing `www.` (no `canonical_url` changed). Their url-group sources
came from collectors that already deduplicate within a channel by slug, and no two channels listed the same page. The
new rules matter for link targets (§3.4 of `docs/design/link-crawling.md`: collisions, `www.` pairs, DOI-in-path
papers) and for manual sources; the synthetic test shows a `www.`/bare merge and an OpenReview split end to end. A
migration run writes one chain: a first attempt that wrote two chains' parts in one run failed before publishing (two
staged files of one family shared a file name). The result table of that run: `docs/repo-history/superseded-records.md`.
