# `collect.links` — outbound links of linking documents, chosen by a context-aware selector

Code: `stack/collect/links.py` (the module docstring is the contract); this directory holds the stage entry
(`main.py`) and the selector prompt (`link-select-v1.md`). Tests: `tests/test_collect_links.py` (synthetic bodies,
a fake model on the LLM client's transport protocol; no network). Design: `docs/design/link-crawling.md`
revision 2, with the changes of decisions D222-D225 in `docs/DECISIONS.md`.

    STACK_ENV_FILE=.env uv run --no-sync stack run collect.links --plan <plan> --in data/sources.links/<series>/ \
      --in data/coverage.links/<series>/ --in data/documents/<series>/

Plan (required: the prompt pin, `miss_cap: 1`, `budget.max_usd`):

    prompts:
      collect.links: {name: link-select-v1, model: openai/gpt-6-luna}   # optional: role, max_tokens, template_digest
    stages:
      - {stage: collect.links, prior: [sources, coverage], miss_cap: 1, budget: {max_usd: 0.3},
         options: {since: "2026-09-12"}}   # optional: rules, exclude_kinds, only, max_linking, batch_size, max_changed_targets

In a build it runs after the collectors that mint linking bodies (`collect.feeds`, `collect.manual`) and before the
lookups, so its `links_paper` rows get OpenAlex/S2 records and its `links_social` rows are what `collect.x-lookup`
hydrates (from the next gather's `documents` head).

## Where found links are stored

| What | Where | How to read it |
|---|---|---|
| Every link found in a linking document, with its decision (`selected`, `rejected`, `undecided`), who decided (`rule`, `llm`), the reason (`rule:<kind>` or the selector's ≤ 120-character reason), the selector's kind, model and prompt, and every occurrence (href, fetch URL, anchor, context, heading, position); plus every link dropped as noise (`share`, `asset`, `app`, `button`, `subscribe`, `comment`, `self`) | one **decision record** per linking document (JSON in a blob, `blobs/<sha256>`), written whenever that document's decisions change | the `coverage.links` row on the linking member's channel: `query = {"links_of": <linker source_id>, "decisions": <blob digest>}`, `seen` = candidates, `kept` = selected, status `ok` (complete) or `partial` (undecided items, asked next run). `stack.collect.links.decision_rows(<store>)` flattens the newest record of every linking document into rows |
| Each **selected** target | one `sources` row on `sources.links`, channel `links_paper` / `links_forum` / `links_url` / `links_social` by the target's identity group | typed keys plus one non-merging `relay` key per linking document (its URL); the `provider_record` in-band entry (D522) = `{target, linked_from}` (every linking document with its occurrences and decision); `metadata.title` = the first anchor that reads as a title, `canonical_url` = the earliest occurrence's fetch URL, both first-minted (kept from the row's previous version while it is not retracted: a new linking document adds a relay key and occurrences only); retracted when no linking document selects it any more |
| Cached LLM decisions | the decision records themselves: a decision is reused when prompt digest, model, target key and occurrence digest match | a re-run over unchanged documents makes no call and writes nothing |

## Implemented

- Linking documents by the `links` source rules of `stack/rules.py` (`newsletter_links`: `zvi_substack`, `import_ai`,
  `paradigm3_news`, without Zvi's non-AI series `ZVI_NON_AI_SERIES`; `flagged_links`: key `flag:collect_links`; `lab_post_links`, `safety_org_links` listed, off).
- Bodies: the newest in-band `post` raw `text/html` entry (feed bodies, UTF-8), else the pointer's raw `text/html`
  snapshot (paradigm3's Astro page: `div.article-body`; other pages: the largest `article`/`main`).
- Extraction with noise drops, the rule filter (`LINK_EXCLUDED_KINDS`: homepage, navigation, video, wiki, shop,
  social profile; `options.exclude_kinds` replaces it), the batched selector (≤ 60 links per call, strict JSON schema,
  cached system prompt), the budget stop with partial records, recomputation (changed rows, retractions, linking
  documents that stop qualifying), the `max_changed_targets` safety valve.
- Downstream: the triage rule `selected_links` (fetch every `links_url`, `links_forum`, `links_paper` document),
  gather ranks the link channels after every other channel (`workflows/gather/merge.py` `PROVIDER_LAST`),
  `stack build` passes the `documents` head (`stack/build.py` `DOCUMENT_COLLECTORS`); the plans `batch-2026-10a`
  (production) and `links-scratch`.

## Not implemented (deferred) and what breaks without it

- **`link_context` in-band entry** (§4.7/§4.11; needs `EXCERPT_RANK` in shared core): triage sees no link context.
  Harmless for `links_url`/`links_forum`/`links_paper` rows (the `selected_links` rule fetches them without asking);
  a link document that merges with a document the question gate asks about gets no extra signal.
- **Metadata probe `collect.page-probe`** (§4.10): a selected web page has only its anchor as a title (or none) and
  no date until acquire fetches it; keyless paper landing pages stay in the `url` group (no `citation_doi` re-key).
- **`collect.lw-lookup`**: a `links_forum` row is fetched through acquire's http lane (plan `links_forum: [post]`)
  instead of the GraphQL body.
- **Per-channel recheck** (`acquire.recheck` per channel): a fetched link page is rechecked weekly like every `url`
  page (the plan's `recheck.default`).
- **Acquire carry rule** (§4.11): a link member and a collected member of one document can both want a fetch.
- **Tweets**: `links_social` rows are hydrated only when the plan runs `collect.x-lookup` (not in `links-scratch`).
- Shorteners (`t.co`, `bit.ly`, `ft.trib.al`) are url targets keyed on the short URL (no network here).
