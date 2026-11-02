# Triage eval set `sr2025-recall` (recall only)

Task: the triage gate's LLM question (`workflows/triage/ask.py`), as for `2026-10-12a`. **Every item is a Shallow
Review 2025 inclusion**, so its gold is `relevant` / `fetch: true`. The set measures recall of the SR scope
(`docs/scope/shallow-review.md`); precision cannot be measured here (there are no negatives). Built 2026-10-12 by
`python -m stack.evals.sr recall` (track E2; `manifest.json` has the code digests, the documents head
`rfm2d6vzwvmdd.documents` of `v4/store-dev` (gitignored; not in the repo), the sampling and resolution counts). Gold scope is `null` (not
labelled `core` / `sr_only`).

## Source
SR's public included-items file `main-pipeline/data/2025-12-16-draft-post-review/papers.csv` from
`github.com/arb-consulting/shallow-review-2025`, fetched 2026-10-12 16:32 UTC, copy in `source/papers.csv`
(sha256 `29575445…`, 826 rows = agenda x link, 11 columns; `source/fetch.json`). One row has no link. 760 unique
links once a link listed under several agendas is merged (kept as `sr_agendas`; `stratum.sr_section` /
`sr_agenda` are the first row's).

Link kinds (rows / unique): arxiv 486 / 440, lesswrong 142 / 136, org_site (research-org, personal and Substack
pages) 64 / 63, lab (frontier-lab sites) 58 / 51, other (GitHub, Google Docs, t.co, YouTube, …) 26 / 24,
openreview 19 / 18, alignmentforum 17 / 15, journal (DOI or publisher) 13 / 12, none 1 / 1.

## Sampling and resolution
150 items, seed `track-e2-sr2025`, order `sha256(seed + kind:key)`: arxiv 95, forum 30 (lesswrong 27,
alignmentforum 3), other 25 (journal 7, openreview 6, org_site 6, lab 3, other 3); the forum and other groups first
take 3 per kind, then the group's pooled hash order. Split 75 / 75, 50/50 inside each kind.

Each link is resolved to the record production's triage question would see, `document_text(row, excerpt)`:
- **in `v4/store-dev`** (arXiv id, DOI, ForumMagnum id or URL match): the store row's own frozen item, `origin:
  store`. One item (`sr-0045`, a semantic_scholar-anchored paper); its externally built record was byte-equal to
  the store's. 21 lab links are in store-dev as url documents without abstract or excerpt (production fetches
  them without asking the question) and were dropped.
- **arXiv** (94 arXiv links, 3 of them relinked by the title check below; 14 other links resolved by title):
  the Atom API, parsed by `collect.arxiv_sweep.parse_entries`;
  excerpt = the abstract minted as the collector mints it. On 5 store-dev arXiv rows this reproduced the store
  record exactly 4 times (the fifth differs in authors merged from another provider).
- **LessWrong / Alignment Forum** (30): GraphQL for the title, then the forum search `collect.forums` reads; the
  hit's title, authors and body minted give production's LessWrong record exactly (all 30 found:
  `excerpt_origin: lw_search_body`; 6 of 6 store-dev LessWrong rows reproduced byte-equal). AF links are given
  the `lesswrong` channel's record, see caveats.
- **other kinds** (25): OpenAlex by DOI or exact folded title (11, `resolver: openalex`). OpenAlex's free daily
  budget for this IP ran out during the build (429, "Insufficient budget … resets at midnight UTC"; shared with the
  collectors). The remaining links were resolved by exact title through the arXiv API (14, `resolver:
  arxiv_title`, channel `arxiv`). Semantic Scholar's unauthenticated pool was tried first as the fallback and
  answered 429 through four retries.

**SR title check** (added on the conductor's finding). Some papers.csv rows point at the wrong paper: the editor's
`link_text` names the intended one while the URL (and SR's `title` column, scraped from that URL) is another
paper. Every resolved record's title is compared with the row's `link_text` (the `title` column only when the
`link_text` is a scraping artefact such as `Unable to access: 429…` or under 12 folded characters): folded
equality, containment, a sequence ratio >= 0.9, or content words shared at >= 0.6 of the shorter title (editors'
paraphrases such as "Opus 4.5's Soul Document"). A mismatch is re-resolved by the `link_text` through the arXiv
title search (`resolver: arxiv_link_text`, `doc_id` the found paper, `sr_link` the original URL) or dropped
(`title_mismatch`) and replaced from its stratum. In this sample (`manifest.json` `title_check.mismatches`):

| item | SR URL | link_text (intended) | URL's paper | outcome |
|---|---|---|---|---|
| sr-0041 | arxiv 2412.02345 | Beyond the Binary: Capturing Diverse Preferences With Reward Regularization | quantum gates (Tracy-Singh product) | relinked to 2412.03822 |
| sr-0106 | arxiv 2507.06789 | The Curious Case of Factuality Finetuning: Models' Internal Beliefs Can Improve Factuality | spectral Barron functions | relinked to 2507.08371 |
| sr-0130 | arxiv 2509.03405 | Reasoning Introduces New Poisoning Attacks Yet Makes Them More Complicated | LMEnt (knowledge in LMs) | relinked to 2509.05739 |
| — | arxiv 2510.24797 | Self-Correction in Thought-Attractors: A Nudge Towards Alignment | LLMs report subjective experience under self-referential processing | not on arXiv by that title: dropped, replaced by sr-0135 (2504.01871) |

Against the first freeze (same seed), only these four links changed; the drop and the replacement renumbered and
re-split 58 items (hash order), nothing had run on the set. The check covers the 150 sampled items (none
unchecked), not the whole csv.

Drops (title-only, not found, or store title-only), each replaced from the same stratum: lab 41 of 44 attempted
(21 store title-only, 17 not in OpenAlex, 3 not on arXiv), org_site 49 of 55, other 19 of 22, openreview 8 of 14,
journal 5 of 12, arxiv 1 of 96 (the title mismatch above); lesswrong, alignmentforum none.

Licence class: the store row's for the store item (`restricted`); else the `channels.yaml` licence class of the
channel whose record the item replicates, which is how `collect.base` sets a member's: arxiv, lesswrong and
openalex are all `open` (149 items).

## Caveats
- External records approximate production's for documents our collectors have not seen: the abstract and authors
  are one provider's, where a store row merges several (authors can differ), and a document production would
  first meet through another channel would carry that channel's record.
- **Alignment Forum.** An AF post anchored on its `collect.af_graphql` member has only raw HTML in-band, so
  production has no excerpt and does not ask the question (store-dev's one AF-anchored document is title-only).
  The 3 AF items carry the `lesswrong` channel's record, which production has only when the LessWrong member is
  the anchor (gather anchors on the earliest observed member). `collect.lw_graphql` (in progress, track A2) also
  mints `htmlBody` raw.
- The 25 "other" items are biased toward documents with a scholarly record (OpenAlex or arXiv): lab and org pages
  rarely have one, and SR's URL-only items are mostly unaskable by production anyway.
- SR includes items for agenda reasons (a lab's output, a field's framing), so a few positives may fall outside
  our rubric; a miss on such an item is not necessarily a model error.
- SR's own titles are used only to find and check records; the record text is the provider's.
- The title check catches a wrong URL only where the editor's `link_text` is a real title; for forum and "other"
  items a paraphrased `link_text` passes on word overlap, so a wrong link with a similar title would pass too.

## Use
`STACK_ENV_FILE=../repo/.env uv run --no-sync stack eval triage --set sr2025-recall --split dev --models <ids>
--max-usd X`; `stack eval score` reports recall (both `fetch_on`) and the skipped list for a recall-only set.

## Results 2026-10-12 (track E2; runs under `v4/evals-store/triage/sr2025-recall/`, gitignored; not in the repo)

Recall of SR's inclusions under `fetch_on: [relevant]` / `[relevant, borderline]` (after the link-text fix; deepseek
skips the one `restricted` item on dev):

| model | v2 dev | v2 test | **v3 dev** | **v3 test** | $/1k (v3) |
|---|---|---|---|---|---|
| openai/gpt-6-luna | 90.7 / 90.7 % | 86.7 / 86.7 % | 92.0 / 93.3 % | 92.0 / **97.3 %** | 0.12-0.28 |
| anthropic/claude-haiku-5.5 | 89.3 / 93.3 % | 84.0 / 90.7 % | 89.3 / 97.3 % | 92.0 / 98.7 % | 0.17-0.47 |
| deepseek/deepseek-v4.1-flash | 87.8 / 87.8 % | 89.3 / 90.7 % | 91.9 / 95.9 % | 97.3 / 97.3 % | 0.11-0.12 |

Before the link fix two "misses" were SR's own URL errors (sr-0041, sr-0107: link text names one paper, URL points to
a quantum-gates and a Barron-functions paper); every model skipped them. luna-v3's 6 test skips under
`[relevant]` (2 remain under `[relevant, borderline]`) are singular-learning-theory, representation-alignment and
backdoor-theory papers whose abstract carries no safety framing ("Programs as Singularities", "Compressibility
Measures Complexity: MDL Meets SLT", "Aligning Machine and Human Visual Representations…", "Oblivious Defense in ML
Models…"), the kind SR files under developmental interpretability, representation structure and human inductive
biases. Precision is not measurable here; see `2026-10-12a`
(gold v2) for it. Also usable from SR's repository (`docs/scope/sr-2025-repo-criteria.md`): the arXiv sweep (368
papers with abstracts, 150 with a curation verdict) as a small labelled set with real negatives — not built.
