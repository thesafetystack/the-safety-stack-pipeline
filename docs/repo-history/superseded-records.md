# Superseded records

Passages moved out of the current docs because they describe an earlier state: estimates made before the runs they
estimated, results on development stores of the time, provenance of the design, and items since resolved. Each
section says where it came from. The current docs do not depend on anything here.

## DESIGN.md: how the design was made (header as written 2026-10-12)

Written as an exploration (2026-10-12). "v4", "v4e", "v4f" are working labels. Provenance: branches B1 (chain
semantics), B2 (versions), B3 (fold, consumers); seven Opus critics; eight Codex answers; the contract
`review/reconciled.md` (E1-E22, C1-C36), which is in the author's notes, not in this repository: a `(C<n>)`, `(E<n>)`
or `F<n>` citation marks where a rule came from, and the sentence it annotates states the rule. "Ran" = the B2
partials mock, the B3 fold mock or a critic's probe (cited by file and finding); the rest is argued. v3 file:line at
9312c8c as verified by the source named, C35's corrections applied.

## The full run: estimates, S2 timing, open choices (`docs/howto/full-run.md`, as written 2026-10)

**What to expect (estimates, D278, not measured end to end).** Estimates for std-v3, made before the first full
fetch; the full fetch's measured runs are in the store's run records.

- Size: ~105-140k documents after gather (OpenAlex alone ~144k group hits since 2025-10-01 before dedup).
- LLM: triage ~$14-18, screen ~$30-90 (capped at $3 per 1,000 documents by `screen_usd_per_1k`), affiliation
  ~$14-45; total ~$60-150 for the whole backfill. There is no $150 cap: the host-wide ledger that held it was
  removed (D328); the caps are the per-run ones.
- Wall time ~3-5 days: triage ~8 h, acquire ~2-3 days of builds (arXiv's 3 s per request and the sequential oa
  lane), screen 9-15 h, affiliation 4-7 h.
- Disk: local parquet a few GB; the blobs a container fetches stay in its mirror until it is discarded, ~100-150 GB
  for the backfill (an estimate from the S3 store's design; less with the acquire option `arxiv_pdf: on_miss`, which
  std-v3 does not set). In the bucket: ~55-80 GB by the rehearsal's extrapolation; an earlier estimate said ~100-170
  GB of PDFs.
- `stack verify` runs in every build; its memory was bounded in October 2026.

**The standalone S2 collect's expected time** (D550, a virtual-clock test of 62 pages at one request per 2.5 s):
~3 min without 429s, ~6 min at 25 %, ~24 min at 49 % (the e2e took 61 min).

**Open choices.** The decisions marked "default taken" in D236-D279. Two are superseded in `batch-2026-10a`: D269
kept the new annotators out of the production plans (model_roles runs there since revision 7, D710, beside
technique_staged; technique_roles stays in `ann-scratch`), and D277 resolved OpenAlex author ids but left them off
(`collect.openalex` sets `author_ids: true` there).

## URL re-key: the result on the development stores (`docs/design/url-normalisation.md` §3, 2026-10-12)

Both stores were backed up (`data/` + `runs/`) before applying.

| | `v4/store-dev` | `v4/store-sr-scratch` |
|---|---|---|
| sources rows (view) | 2,013 | 5,231 |
| rows re-keyed | 5: anthropic_cards 5 | 497: uk_aisi_blog 163, far_ai_blog 109, forethought_blog 78, goodfire_blog 61, cooperative_ai_blog 29, arc_blog 28, apollo_blog 24 (37 of its 61 already bare-host sitemap links), anthropic_cards 5 |
| what changed | the `url` key loses `www.` (`www.anthropic.com/claude-…-system-card` → `anthropic.com/…`); no `canonical_url` changed | the same, every row; no `canonical_url` changed |
| baseline prediction vs head | 1,806 = 1,806 documents, 0 merges, 0 splits | 4,898 = 4,898, 0, 0 |
| re-keyed prediction vs head | 0 merges, 0 splits | 0 merges, 0 splits |
| runs | rekey `r2vvrnanx1p7e`; gather `rhj8fc5z0xtrn` (dev-v2-m4): 5 changed, 1,801 carried | rekey `r6h16fnqwhd84`, `rrra41j0vwzqa`; `fold.compact` upcast of `documents` `rk8azxae7w4dt` (the head was at a pre-D207 schema; gather refused it); gather `rjxyw3dqjdr9k` (dev-v2): 497 changed, 4,401 carried |
| after gather vs prediction | identical membership (1,806 live documents); no state changed | identical (4,898); no state changed |
| unrecovered original URLs | 0 rows without a URL; all 1,411 url-group rows' only URL is the stored canonical_url in v3's collapsed form, none on a host whose key keeps query parameters | 0; 3,040 collapsed, none on such hosts |
| `stack verify` before → after | 1 failed (X6 policy_pins, 321 affiliation evidence rows on a non-pinned template) → the same 1 failed | 2 failed (V11 documents schema, X6 policy_pins 62 screen-v8 evidence rows) → 1 failed (X6, the same 62) |

A first `--apply` on store-sr-scratch wrote both chains' parts in one run and failed before publishing
(`r6738et5dzmf9`, `failed[code_bug]`: two staged files of one family share a file name); nothing reached `data/`.
That is why a run now writes one chain.

## FUTURE.md: items resolved since they were listed

A keyless S2 429 keeping every std-v3 build `incomplete` (std-v3's `collect.s2-lookup` has `require_complete:
false`); the OA lane as one sequential host group (now `fetch_workers - 2` slots, `docs/howto/concurrency.md`); arXiv
HTML only for `arxiv`-channel members (`structured.arxiv_html` covers every member with an arXiv id, D294); an S2 429
inside the OA walk halting the whole lane (S2 is one rung, the lane goes on, D402, D413; it halts only on Unpaywall and
the ID Converter, D164); the v3 archive importing landing pages as PDFs (the archive step is removed, D329).

## The production plan's earlier digests (`docs/DEPLOY-railway.md`, `STACK_SCHED_EXPECT_PLAN_DIGEST`, as of revision 12)

`ff4932034e2e136dd61eb6293d924555da22b9b2f1732f6914869a52173a1af7` for `batch-2026-10a` revision 12 at this commit (D800: the S2 collector's per-query cap `max_records` 250000, was 100000; before it, revision 11 `8c4eb6954977d8d8007dccf28b46be94eb9fcf9fd1416a37d1191583b1405a76` (D779: revision 10's accepted configs, the Chinese coverage's translation stage and feed, organisations 0.2.2 and matcher 4), revision 10 on orgs 0.2.0 was `415b9dfcdca23a1544dc542c8accb79df9636f002de09cb8f2ad82f6385fc56c` (D758), revision 9 `c24a6aef8fc5302a6decaeea18c2ef409488360fc1441c9053165a61a2c57623` (D738), revision 8 with D760 `b6796f5949c7876df4f742986f78da41d3bdb2d5d23039108832ba2150aa6da6`, before D760 `9a0293054e0afd3bb8f09de17dad816d2c23084dc08e0c96582183029d543a27` (D736, D737); revision 7 `a9a43bf0b1b275a161f1f037724a7885fe7ca2815528203f885d596d2ef78ffa`) (`python -c "from stack.plan import load_plan; print(load_plan('configs/plans/batch-2026-10a.yaml').plan_digest)"`). Before the first cycle of revision 11 the operator imports the two prepared override files (a cycle never runs `stack overrides add`): `configs/overrides/china_coverage.yaml` and `configs/overrides/data_quality_2026_10.yaml`, each with `--dry-run` first (D779)
