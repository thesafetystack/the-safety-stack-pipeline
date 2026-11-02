# Add a blog or a feed

There are two collectors for blogs; which one depends on whether the site has a usable RSS/Atom feed with bodies.

| | `collect.feeds` (`kind: feed`) | `collect.blogs` (`kind: scrape`) |
|---|---|---|
| Reads | one RSS/Atom feed per channel | the site's index pages, sitemap or feed, by a per-site spec |
| Bodies | the item's `content:encoded` / Atom `content`, stored in-band as role `post` (no fetch); an item without a body is listing-only and `acquire` fetches the page | none; `acquire` fetches each post page |
| Needs code | no | yes: a `SITE_SPECS` entry in `workflows/collect/blogs/main.py` |
| In which plans | only `configs/plans/links-scratch.yaml` (round 4); no production plan runs `collect.feeds` today | `std-v2`, `std-v3`; `dev-v2`, `dev-v2-m4`, `ann-scratch` (5 channels each) |

## A feed (`collect.feeds`)

1. A channel in `configs/channels.yaml` (next to `zvi_substack`, `import_ai`, `paradigm3_news`):

   ```yaml
   - id: example_newsletter
     kind: feed
     location: "https://example.substack.com/feed"
     license_class: public_web
     channel_group: url
     politeness: { rate_rps: 0.5 }
     # include_re: "^https?://example\\.org/news/"   # optional: keep only matching item links
     # self_published_org: <org slug>                 # optional
   ```

   Bump the file's `version` and run `uv run --no-sync python tools/check_configs.py`.
2. In the plan: a `collect.feeds` stage line, if the plan has none yet (as in `links-scratch.yaml`):
   `- {stage: collect.feeds, out: {kind: part}, prior: [sources, coverage], options: {since: "2026-09-12"}}`
   and the channel in `acquire.wants` (`example_newsletter: [post]` when the feed carries bodies, `[page]` when it
   does not).
3. Triage: the new rows pass the gate only through some clause: the source rule `newsletter_posts` in
   `stack/rules.py` (add the channel to `NEWSLETTER_CHANNELS`) named as `{rule: newsletter_posts}` in the plan's
   `gate:` (only `links-scratch.yaml` names it today), or the LLM question clause. `NEWSLETTER_CHANNELS` also makes
   the channel's posts linking documents for `collect.links` (rule `newsletter_links`, in plans with a
   `collect.links` line); list the newsletter's other publication hosts (a mirror) in `LINK_SELF_HOSTS` there (the
   page's own host always counts as a self-link).
4. Run on a store copy: `stack run collect.feeds` (with `--plan`), then `gather`, then a build.

## A scraped blog (`collect.blogs`)

1. A `kind: scrape` channel in `configs/channels.yaml` (see `miri_blog`, `metr_blog`, …).
2. A `SITE_SPECS` entry with the same id in `workflows/collect/blogs/main.py` (index/sitemap/feed surfaces and the
   link regex). **A `kind: scrape` channel without a `SITE_SPECS` entry is skipped silently** (the stage selects
   `ch.kind == "scrape" and ch.id in SITE_SPECS`); all current scrape channels have one.
3. The channel in the plan's `acquire.wants` (`<id>: [page]`). The plan's `collect.blogs` line already runs every
   scrape channel with a spec (`dev-v2`, `dev-v2-m4` and `ann-scratch` limit it with `options.max_channels`).
4. Triage as above; the safety-org blogs pass by the `safety_org_posts` rule in `stack/rules.py`, which lists
   `SAFETY_ORG_CHANNELS`, so add the new channel there if it belongs.
5. A test in `tests/test_collect_blogs.py` on a saved index page (`STACK_BLOGS_FIXTURES` is the fixture hook).
