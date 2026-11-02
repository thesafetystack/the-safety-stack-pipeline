# Add a collector

A collector is a stage `collect.<suffix>` that observes sources from one provider or site and writes `sources` and
`coverage` rows on its own chains `sources.<suffix>` and `coverage.<suffix>` (one stage may serve several channels:
`collect.blogs` writes every blog channel on `sources.blogs`). The contract and a full skeleton are
the module docstring of `stack/collect/base.py` (the `Collector` harness); read it first. A stage in general:
`workflows/README.md` "Writing a stage".

## Checklist

1. **Module.** `workflows/collect/<suffix>/main.py` exposing `run(ctx) -> StageResult`, built on
   `stack.collect.base.Collector`: `c.option(...)` for plan options, `c.transport(PACING)` for HTTP at the
   provider's own pacing (no global default), `c.window(channel, query, start, end)` per query window,
   `c.observe(channel, external_id, record, keys=[(kind, value)…], metadata={…})` per source,
   `c.add_inband(...)` for a body or abstract that came with the listing, `return c.finish()`. When the provider
   answers with a record of the item (an API's JSON item, a feed entry), pass it as
   `c.observe(..., provider_record=item)`: it becomes the row's `provider_record` in-band entry (canonical JSON;
   raw bytes with `record_media_type=` otherwise; D522, D526), never content. Leave out a body already minted
   in-band and per-query noise. A scraped listing with no API record (blogs, cards) writes none. Keys are typed
   `(kind, value)` pairs in their normal form (`stack.scholarly_records.normalize_*` for DOI, arXiv, PubMed;
   `stack.urls.url_key` for URLs). The duplicate detectors in `stack/identity.py` add their keys inside `observe`.
   The module also declares `OPTIONS`, every key it reads with `c.option` (plus the shared `RELIST_OPTIONS`,
   `SAMPLING_OPTIONS`, `METER_OPTIONS` it uses): the runner refuses any other key of its line's `options` (D434).
2. **Channel.** An entry in `configs/channels.yaml` under `channels:` with at least `id`, `kind`,
   `license_class` and `channel_group` (`paper`, `forum`, `url`, `social`, …, as listed under `channel_groups`;
   it decides which keys merge), plus as needed `location`, `politeness`, `miss_means`,
   `non_merging_keys`, `record_projection` (the record fields whose change counts as a new observation). Schema:
   `spec/schemas/config/channels.schema.json` (it refuses any key it does not list); `uv run --no-sync python
   tools/check_configs.py` validates it.
   Bump the file's `version` and say why in its header comment.
3. **Plan line.** In each plan that should run it:
   `- {stage: collect.<suffix>, out: {kind: part}, prior: [sources, coverage], options: {...}}` (`out: {kind: part}`
   as on every collector line of the plans: without it each run writes a full file). The `prior: [sources, coverage]` is
   required: a run of a collector whose chains have files but are not pinned is refused (it would re-mint every
   `source_id`). Optional: `miss_cap`, `keys` (lookups), `budget`.
4. **Fetching.** If documents of this channel need a body fetched, add the channel to the plan's
   `acquire.wants: {<channel>: [<content_role>…]}` (roles: `full_text`, `page`, `post`, `xml`, `html`, `readme`,
   …; `stack/schemas/vocab.py` `CONTENT_ROLES`).
5. **Triage.** New rows are fetched only if the plan's `gate:` lets them through: a `{rule: <name>}` clause naming a
   source rule in `stack/rules.py` (channels, URL prefixes, authors, …), a `{sql: …}` clause, or the LLM question
   clause. Check which clause would decide your rows.
6. **Test.** `tests/test_collect_<suffix>.py` against fake responses (see `tests/test_collect_openalex.py` or
   `tests/test_collect_robustness.py`: the module-level `http_get_json` and `_sleep` seams are monkeypatched; no
   network in tests).
7. **Try it** on a copy of a store: `stack run collect.<suffix> --in data/sources.<suffix>/<series>/ --in
   data/coverage.<suffix>/<series>/` (with the plan line in `--plan`), then `stack ls data/sources.<suffix>/<series>/`,
   and `stack run gather` with an `--in` for every `data/sources.*/<series>/` with a file, `data/documents/<series>/`
   and, when it has a file, `data/overrides/<series>/` (a build's inputs: `configs/plans/README.md`, "What a build
   line reads and writes"). `tools/probe_collect.py` runs one live query without writing anything.

## Things that bite

- An environment variable named in `c.option(..., env=...)` wins over the plan option and enters no digest; only a
  cap or sampling one (`*_LIMIT`, `*_FRACTION`, `*_MAX_QUERIES`) makes builds and collector runs refuse to start
  without `--allow-env-limits` (D553), and is then recorded (`env_limits`), counted (`env_override.<VAR>`) and warned
  (D445; OPERATE, "Environment overrides"). Avoid adding new ones.
- A run-level 429 (a `retry_after` above the pacing's `halt_429_after_s`) halts the run cleanly (`RunHalted`):
  observations so far are written, the open window is `failed_transient[rate_limit]` with its `retry_after` and no
  attempt charged, the run ends scope-incomplete (build reasons `halted`, `windows_failed`) and the next pass
  retries (`stack/collect/base.py`, "Run-level 429" and "Gate cooldown").
- A collector that runs its own retry ladder over `Transport.get` must not spend a step on the Gate's cooldown
  answer (a 429 with `X-Stack-Cooldown`, body `message` `provider_transport.COOLDOWN_MESSAGE`: nothing was sent;
  `Transport.request` already waited it out up to three times), must retry a 429 no sooner than its stated
  `Retry-After`, and must never retry a plan refusal (D588, D583). `Transport.fetch` does all three; prefer it.
- A drain (SIGTERM, `stack stop`; D570, D571) needs no code in a collector: the harness raises `RunStopped` (a
  `RunHalted`) at the next window, request, or `c.progress(done, total)` with `done < total`; the open window writes
  no row (uncharged, asked again next run), and `c.halts()` or, without it, the runner ends the run with
  `c.finish()`. Do not catch `Exception` around `c.window(...)` or a transport call: it would swallow the stop (and
  a halt).
- Only `ok` windows move the watermark; a `failed_transient` window makes the run incomplete (retried next pass), a
  `failed_permanent` one becomes a build warning.
- A window a cap stopped short (`max_records`, a provider's hit or page limit) closes with `w.truncated()`: it is
  `partial`, and the run ends scope-incomplete with build reason `limit` (D492), also in a later pass that skips the
  unit as not due. A planned `partial` that is not a cap (a stopping run deferring work it reports itself) uses
  `w.truncated(cap=False)`.
- A collector that only re-reads stored sources (a lookup) declares `keys:` on its plan line; see
  `collect.openalex-lookup` and `workflows/fetch/metadata/`.
