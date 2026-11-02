# Duplicates: identity rules, detectors, overrides and URL rules

A document is a connected component of `sources` rows joined by shared merging keys within a channel group
(`configs/channels.yaml` `channel_groups`: `paper` on doi, arxiv, openalex, s2, pmid, zenodo; `forum` on
`fm_post`; `url` on `url`; `social` on `x_post`; `record` on `registry_id`). Gather computes the components;
DESIGN §6 has the model. Three places add to it.

## 1. URL rules: `stack/urls.py`

The one home of URL rules (module docstring; `docs/design/url-normalisation.md`):

- `fetch_url(raw)`: what a lane fetches; `identity.canonical_url` holds it (redirect wrappers unwrapped, tracking
  parameters dropped, path kept).
- `url_key(raw)`: the `url` merging key (https, no `www.`, mirror hosts folded via `HOST_ALIASES`, identity query
  parameters only via `KEY_PARAMS`, domain canonical forms for arXiv, DOI, X, YouTube, OpenReview, Substack,
  Medium, GitHub, forum posts, Google Docs).
- `link_keys(raw)`: what a link points at: group, typed keys, fetch URL, kind.
- `alternate_url_keys(raw)` / `ALTERNATE_URL_RULES`: other URL forms of the same page that `url_key` cannot fold
  (an Alignment Forum post also at `lesswrong.com`) or should not fold (`MOVED_POST_PATHS`, D772: a site's posts
  moved to another path or host, e.g. `openai.com/blog/<slug>` → `/index/<slug>`, so a linkpost written before the
  move joins the post; an alternate only adds a key, where folding would change stored ones).

After changing a rule: `stack migrate rekey` (dry run: rows re-keyed, merges and splits), `--apply`, then
`gather` (`docs/OPERATE.md`, "URL rules and re-keying"). Not implemented: shortener resolution (`t.co`, `bit.ly`),
`substack.com/home/post/p-<n>` mapping, PMC ids as keys.

## 2. Duplicate detectors: `stack/identity.py` (D251-D253)

All detectors are declared in this one module (module docstring):

- **Key adders** (`KEY_ADDERS`, per record): extra typed keys read from a source's own record. They run in
  `Collector.observe` and in `stack migrate rekey` (which adds, never removes). Current: `lw_linkpost` (a
  LessWrong/AF linkpost gets its target's key, never a paper's), `feed_guid`, `openalex_arxiv_location`,
  `url_alternates`.
- **Cross-group keys** (`CROSS_GROUP_KEYS = {forum: {url: url}}`): a forum source's `url` key joins the url group. A
  bridge key carried by two different posts joins nothing (counted `bridge_ambiguous`).
- **Matchers** (`MATCHERS`, cross record, in gather's union): `paper_title_author` (equal normalised title, equal
  first-author surname and first initial, published within 366 days, not two different arXiv ids) and
  `post_title_author` (equal title, within 7 days, same author where both have a byline, else same channel).
  Generic titles (`GENERIC_TITLES`), short titles and title buckets of more than 8 works are skipped (a work: the sources key edges already join,
  counted once; D441). A matched pair is a member edge `match:<name>`.

To add a detector: a function in the right list, a test in `tests/test_identity.py` (and
`tests/test_gather_identity.py` for the gather edge), then `stack migrate rekey` (adders) and `gather` on a store
copy; check the gather counts (`match_pairs_<name>`, `joined_by_<edge kind>`, `bridge_ambiguous`,
`match_crowded_<class>`, and `moved_out`: members that left a document which keeps its id, which `split` does not
count). A second gather must touch 0 rows.

## 3. Human overrides: `identity` overrides

Added with `stack overrides add identity.yaml --out data/overrides/<series>/` (a YAML list in the format below) and read by gather from the
`overrides` chain it pins (`stack/overrides.py`, `workflows/gather/overrides.py`; how to add, revoke and what wins:
`docs/howto/overrides.md`). Retire an entry with a later one that `revokes: <override_id>` or `supersedes` it; of two
entries for the same source set the later wins.

```yaml
- override_id: sep-2026-10-13-1
  author: tomas
  note: two LW users posted the same title
  value: separate                 # remove computed key and match edges between these sources
  key: {source_ids: [s0h3jnptj7a30, s1xyz…]}
- override_id: merge-2026-10-13-1
  value: merge                    # join sources no detector joins (may cross channel groups)
  key: {source_ids: [s…, s…]}
- override_id: mask-2026-10-13-1
  value: mask                     # the member stops being live; the document is masked when no member is live
  key: {source_id: s…}
  mask_reason: operator           # or review_required
```

A `separate` whose sources stay connected through another path masks the document `review_required`. An entry
naming a `source_id` no input chain has is refused. Source ids: `identity.members[].source_id` of the document
(`docs/howto/why-missing.md`, "Find the document"). `fields` (one metadata field of one document) and `<wf>`
(annotation) overrides are added the same way.
