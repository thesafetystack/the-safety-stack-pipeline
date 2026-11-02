# Development history

This repository starts from a single snapshot commit (plus a commit that adds the data
repository as a subtree). The work that led to it took place between June and November 2026
in an earlier repository, which is kept private with its full history (see
[The archived repository](#the-archived-repository)). This document records, from that
history, how the pipeline was developed and who did the work, including work whose code or
data is no longer in the current tree.

It was compiled from the commit log of all branches and archive tags of the earlier
repository (1,098 commits, 23 June to 31 October 2026) and from the 25 commits made after it
up to the snapshot (1–2 November 2026): author names, dates, commit messages, file
statistics, and line-level authorship of the files that survive. Contributions that were made
outside git (review comments, design input, work absorbed from other branches) are listed
where the commit history records them.

## Contributors at a glance

| Person | Commits | Active in git | Main areas |
|---|---|---|---|
| Tomáš Gavenčiak | 1,069 | 23 Jun – 2 Nov 2026 | demos (xray, strata), technique taxonomy import, v3 specification and core module, v3 workflows, the v4 file-based pipeline, evaluations, catalogues, dataset releases, Railway deployment |
| Dan Elton | 53 | 24 Jun – 8 Oct 2026 | the v1 pipeline and its corpus, the v1 dashboards, v3 acquisition (fetch, OA full text, conversion), Semantic Scholar collection, affiliation extraction and its evaluation, the screening/practice-extraction experiment, the atlas and the one-lab deep dive |
| Jacob Livingston Slosser (Jake) | none under his name | July 2026 | the taxman technique-catalogue workstream (taxonomy v0.8 and its extraction pipeline), design review of the v3 specification, review of the taxonomy v0.4–v0.6 facet design |

Identities merged: "Tomas Gavenciak", "Tomáš Gavenčiak" and "Tomas Gavenciak (via Claude)"
are one person; Dan Elton's commits under two author identities are counted together. One merge commit
(21 July) is authored by the identity "Claude"; it belongs to a Claude Code session in the v3
build and is counted under neither person. Most commits were made with Claude Code: Claude
co-author trailers appear on 1,017 of Tomáš Gavenčiak's commits and on 40 of Dan Elton's.

## Timeline

### 1. Repository start and the v1 pipeline (23 June – 17 July)

- 23–24 June: repository created (Tomáš Gavenčiak). Dan Elton's initial import brought in the
  v1 pipeline: researcher-roster-based arXiv pulls, OpenAlex and other API searches,
  abstract and full-text fetching, PDF-to-Markdown conversion, LLM screening, affiliation
  extraction and metadata backfills over a SQLite corpus database; a web app over the
  corpus (authors, organisations, pipeline views); an early `taxonomy.yaml`; the screening
  methodology document and the project plan.
- 2–17 July (Dan Elton): repeated restructuring of the v1 pipeline into named stages (one
  file per stage, a shared library, configuration and prompts in their own directories);
  a run-control dashboard with per-stage runs and a corpus funnel; the broad v2 screen
  (33.4k documents screened, 6.2k included as of 9 July); per-lab system-card scrapers;
  an OpenAlex sweep by institution ids; vendored PDF conversion; a drop folder for manual
  ingestion and missing full texts; the corpus database tracked in Git LFS (19 July).
- Main contributor: Dan Elton.

### 2. Taxonomy import and demos (29 June – 2 July)

- 29 June: the alignment-technique taxonomy, developed outside this repository until then,
  imported as `taxonomy/`: v0.6 (292 techniques including 49 variants, a 15-facet
  dictionary), the v0.1–v0.5 history, design documents and a source corpus of about 2,000
  converted model cards and papers with its extraction scripts (Tomáš Gavenčiak, with
  Claude Code). The design notes record Jacob Livingston Slosser's review points as the basis of the
  v0.4–v0.6 facet redesign (single-basis axes, the granularity rule).
- 30 June – 1 July: two demos (Tomáš Gavenčiak, with Claude Code). **xray**, a
  deployment-matrix prototype, graded 518 in-scope documents with an LLM. **strata**, a
  model-centric knowledge base with a Vite app: a model registry, classification of 1,546
  arXiv papers as technique evidence, web commentary (516 posts) and independent
  evaluations (113 evaluator documents) as further evidence sources.
- 2 July: Dan Elton separated his model-cards dashboard demo from the pipeline dashboard and
  made the demos self-contained.

### 3. The v3 specification (18–20 July)

- A phase-interface specification written over review rounds A–L (Tomáš Gavenčiak,
  with Claude Code): goals and desiderata, an interface design (glossary, phase contracts,
  database DDL, configuration schemas), a technical layer (algorithms, payload schemas,
  service interfaces), lint and DDL smoke tools, and a decision log.
- The specification was organised around the two existing workstreams: retrieval (Dan
  Elton's v1 pipeline, which the interfaces were walked through against) and annotation and
  taxonomy (Jacob Livingston Slosser's taxman). Its phase-cleanliness principle generalises a
  retrieval-stage improvement spec shared with Dan Elton.
- 20 July, review round L: five comments from Jacob Livingston Slosser, recorded and verified in the
  specification's review files. Adopted: granular version-bump rules so that adding a
  technique to the taxonomy does not force full re-extraction (L-1), a cheaper model for the
  extraction role (L-2), and a consistent extraction-depth rule (L-5); his proposal on
  model-ruled identity clusters (L-3) led to a two-tier adjudication rule.
- 20 July: Jacob Livingston Slosser's taxman work (branch `jls-taxman`) absorbed byte-exact as
  `taxman/`: taxonomy v0.8 (the same 292 technique slugs as v0.6, 10 facets, facet answers
  with values and origin), its builders, extraction, support-check and index pipeline,
  shared LLM and quote-anchoring code, and data contracts.
- 20 July: the `stack` core module (types, ids and hashes, database, blob store, read API,
  validated writer; 295 tests) and the earlier "fabled" design prototype imported for
  reference (Tomáš Gavenčiak, with Claude Code).

### 4. The v3 build (20 July – 27 July)

- Built by Claude Code sessions committing as "Tomas Gavenciak (via Claude)" and merged
  through pull requests #1–#3: an LLM client, quote anchoring, a workflow runner and CLI;
  the v1 corpus importer; steel-thread workflows (document class, screen, funnel report);
  collectors for arXiv, forums, the Alignment Forum (GraphQL), OpenAlex, blogs and seven
  labs' model cards and policies; scholarly metadata fetch; HTTP acquisition with SSRF
  guards; tiered Docling conversion carrying over v1's guards; a 110-entry weak-spot audit.
- The v3 configuration ported v1's search set: the 42 concept groups (146 terms) and the
  researcher roster derived from v1's tracked-researcher list, both originally Dan Elton's.
- 27 July: v1-import fixes and a full-text quality review of the imported corpus (Tomáš
  Gavenčiak).

### 5. v3 acquisition, affiliation and evaluation (26 July – 2 September)

- Dan Elton, mostly with Claude Code: an open-access PDF waterfall (OpenAlex, Crossref,
  Semantic Scholar; pull request #4); the Semantic Scholar collector, which covers the
  misuse concept groups that OpenAlex queries cannot express; HTML/JATS acquisition (arXiv
  LaTeXML and publisher XML) and table handling in conversion; affiliation extraction as an
  annotation, organisation attribution, organisation-name canonicalisation and a
  deterministic XML byline tier.
- Affiliation evaluation (Dan Elton): a truth set of 200 papers and 1,502 authors graded by
  a stronger model, scoring both v3 and v1 against it; a later revision answered a
  methodology review with paired significance tests, symmetric tier correction and a
  measured PDF-conversion-loss ceiling.
- Test infrastructure on a real PostgreSQL with per-run schemas and parallel runs; the test
  suite added to the overall check (Dan Elton).
- 26 August – 2 September: a fetchpdf-backed OA full-text workflow partitioned from HTTP
  fetch, separate fetch lanes (arXiv, HTTP, OA), a two-tier screen, funnel deltas and the v3
  pipeline dashboard (Dan Elton). 31 August: v1 pipeline and the fabled prototype moved
  under `old/`.

### 6. Experiments: relevance screen, practice extraction, atlas, deep dive (16 Sept – 8 Oct)

- Dan Elton, with Claude Code: a high-recall relevance screen for documents describing
  safety practices at frontier labs, multi-practice extraction with a production schema, a
  screen evaluation tool, and metadata enrichment, conversion-ladder and provider-transport
  modules in `stack/` (16 September).
- An "atlas" dashboard of extracted practices by lab and document type, and a screening
  methods page reconciled with it (8 October).
- A one-lab safety-stack deep dive (8 October): a document registry and recall funnel, a
  quote-verified sheet of 67 practices, threat views, adjudication of 523 automated findings
  as a first gold set for the automated extraction, and a feasibility memo.

### 7. The v4 file-based pipeline: design and first build (10–14 October)

- Tomáš Gavenčiak, with Claude Code, in parallel agent tracks merged daily. 10–11 October:
  an audit of the v3 pipeline and its data, a parquet review, design rounds with offline
  mocks, the design proposal, an implementation plan (milestones M1–M5) and the decision log.
  Dan Elton's v3 tip was merged into the v4 branch as the design reference (12 October).
- 12 October: store layout, schemas and parquet writer, stage runner and run records,
  checks framework (47 checks), CLI; collectors on a common harness (arXiv, OpenAlex,
  Semantic Scholar, LessWrong/Alignment Forum, blogs, cards, X post lookup); gather
  identity and merge; rule- and LLM-based triage; acquisition lanes and conversion; the
  annotation framework and screen; the LLM client with response cache and cost ledger;
  build, fold, diff and release; the first end-to-end build and release.
- Evaluation sets from 12 October: frozen triage, screen and affiliation sets with two
  model labellers, adjudication and agreement statistics; later model-role and technique
  annotation sets.
- 13–14 October: S3-backed store (records, store index, series layout, jobs with recovery),
  review-fix rounds, a researcher roster with OpenAlex author ids, a repository and docs
  audit (v1/v3 leftovers, demos and Dan Elton's experiment directories moved to `attic/`).

### 8. Annotations, catalogues and the backfill (15–24 October)

- Annotation stages: model roles (merged with model mentions), technique annotation
  (`technique_staged`), affiliation; prompt and gold-set revisions with blind audits.
- Technique taxonomy v0.9 and v0.9.1 (234 techniques, with a slug crosswalk from v0.8) and
  the model catalogue v0.9; both released as catalogues 1.0.0 on 24 October.
- The full fetch plan `batch-2026-10a`: backfill to 2020, a pre-2020 scope review, keyword
  cuts and venue rules, transport and rate-limit fixes, faster verification.
- Release format 2 and the internal export (20 October); a public release restricted to
  screen-relevant rows (23 October).
- Main contributor: Tomáš Gavenčiak, with Claude Code.

### 9. Dataset releases and Railway deployment (25–31 October)

- The dataset release format with card, FORMAT, QUALITY and licence files, payload schemas
  and a shipped vocabulary; judge-based quality evaluation in the release.
- A scheduled weekly cycle on Railway (26 October): a transient container, preflight
  checks, severity-graded summary mail, tagged releases on publish, balance checks and a
  watchdog; later memory telemetry and worker-pool bounds for the container.
- An organisation vocabulary and organisation annotation; Chinese organisation and
  researcher coverage; an X feed collector; acceptance policies for annotation pins.
- Main contributor: Tomáš Gavenčiak, with Claude Code.

### 10. Repository slimming and preparation for publication (27 October – 2 November)

- 27 October: the repository reduced to the v4 pipeline. `attic/` (v1 pipeline, v3
  leftovers, taxman, demos, the taxonomy history) and `dan_experiment/` removed; the data
  repository (taxonomies and catalogues) added as a subtree at `data-repo/`.
- 29 October: the catalogue envelope (techniques 1.1.0, models 1.1.0, organisations 0.2.0).
- 1–2 November: documentation brought in line with the code, eval-set full texts moved out
  of the repository behind a fetch tool, and references to private notes removed.

## Contributors

### Tomáš Gavenčiak

1,069 commits, 23 June – 2 November 2026 (1,017 with Claude co-author trailers; 38 made by
Claude Code sessions under "Tomas Gavenciak (via Claude)").

- Import of the technique taxonomy v0.6 with its history, design documents and source corpus
  (June). Removed from the tree in October 2026; preserved in the archived repository.
- The xray and strata demos (June–July). Removed from the tree in October 2026; preserved in
  the archived repository.
- The v3 specification: goals, design, technical layer, schemas, DDL, decision log and
  review records (July). Parts survive under `spec/`; the rest is preserved in the archived
  repository.
- The v3 `stack` core module and the v3 workflows (collectors, importer, fetch, convert,
  screen), through Claude Code sessions (July).
- The v4 file-based pipeline (October): design and plan, store and runner, collectors,
  gather, triage, acquisition lanes, conversion, annotation stages, checks, build, fold,
  release and export, the S3 store layout, the decision log.
- Evaluation sets and scorers for triage, screen, affiliation, model roles, technique
  annotation and release quality.
- Technique taxonomy v0.9/v0.9.1, the model catalogue, the organisation vocabulary and the
  catalogue envelope; the researcher roster with OpenAlex author ids.
- Dataset release format, the Railway scheduled cycle, and the clean-up for publication.

### Dan Elton

53 commits, 24 June – 8 October 2026 (40 with Claude co-author trailers), including the
merge of pull request #4.

- **The v1 pipeline** (June–July): researcher-based arXiv and API searches, abstract and
  full-text fetching, PDF-to-Markdown conversion, LLM screening, affiliation extraction,
  metadata enrichment, per-lab system-card scrapers, an OpenAlex institution sweep, and the
  stage runner. This was the production retrieval backbone that the v3 specification was
  designed against. Moved to `old/` in August and removed from the tree in October 2026;
  preserved in the archived repository.
- **The v1 corpus** (SQLite database, tracked in Git LFS): 33.4k screened and 6.2k included
  documents as of 9 July; imported into v3 by the v1 importer. Removed from the tree in
  October 2026; preserved in the archived repository.
- **Search configuration and roster**: v1's 42 concept groups and its tracked-researcher
  list, from which the v3/v4 channel configuration and `configs/rosters/researchers.yaml`
  were derived.
- **Web app and dashboards**: the initial corpus web app, the model-cards dashboard demo,
  the v1 run-control dashboard with corpus funnel, and the v3 pipeline dashboard. Removed
  from the tree in October 2026; preserved in the archived repository.
- **v3 acquisition** (July–September): the OA PDF waterfall, HTML/JATS acquisition,
  fetchpdf-backed OA full text with a licence guard, separate fetch lanes, HTTP transport,
  media-type handling, the fetch cycle, the conversion ladder. Partly still in the tree:
  `configs/fetchpdf_ladder.json`, `stack/http.py`, `stack/arxiv.py`, `stack/mediatype.py`,
  `stack/conversion/ladder.py`, and parts of `stack/provider_transport.py`,
  `stack/scholarly_records.py`, `stack/oa_adapter.py` and `stack/fetchcycle.py`, revised in v4.
- **Semantic Scholar collector** for the misuse concept groups; still in the tree in revised
  form (`workflows/collect/semantic_scholar/`).
- **Affiliation extraction and attribution**: the affiliation annotation, its prompt,
  organisation canonicalisation (`stack/canon.py`) and the XML byline tier; still in the
  tree in large part (`workflows/annotate/affiliation/`, `stack/canon.py`).
- **Affiliation evaluation**: the 200-paper truth set graded against v1 and v3, the
  methodology revision and the PDF-conversion-loss measurement. Removed from the tree in
  October 2026; preserved in the archived repository.
- **Test infrastructure**: real-PostgreSQL per-run schemas, parallel test runs, the suite in
  the overall check; collection-robustness, fetch and affiliation tests, several of which
  survive in `tests/`, and `tools/probe_collect.py`.
- **Screening and practice-extraction experiment, atlas and one-lab deep dive**
  (September–October): see phase 6. Moved to `attic/` and then removed from the tree in
  October 2026; preserved in the archived repository. His v3 tip was the design reference
  merged into the v4 branch.
- **Design input**: the retrieval-stage improvement spec that the v3 phase-cleanliness
  principle generalises; the v3 specification's interfaces were validated by walkthrough
  against his v1 fetcher.

### Jacob Livingston Slosser (Jake)

No commits under his name; his work entered the repository through commits by Tomáš
Gavenčiak that record it.

- **taxman** (branch `jls-taxman`, absorbed 20 July): the technique catalogue taxonomy v0.8
  (292 techniques, 10 facets, facet answers with values and origin), its builders,
  extraction, support-check and index pipeline, a shared LLM client, quote anchoring and
  data contracts. Moved to `attic/` and then removed from the tree in October 2026;
  preserved in the archived repository. Taxonomy v0.8 was the menu of the first v4 technique
  annotation stage and the base from which v0.9 was mapped by a slug crosswalk.
- **Review of the v3 specification** (round L, 20 July): five comments, of which the
  taxonomy-addition cascade (L-1), the extraction-model assignment (L-2) and the
  depth-rule fix (L-5) were adopted and L-3 shaped the identity-cluster adjudication.
- **Taxonomy design review**: the design notes for v0.4–v0.6 record his points as the
  source of the single-basis facet split and the granularity rule.

### Others

- Gavin Leech: the taxonomy design notes name him as the source of a curated list of 2026 safety
  papers proposed as a recall seed; he has no commits in the repository.
- The commit trailers name the Claude models used through Claude Code (Fable and Opus
  versions); the one commit authored as "Claude" is a merge in the v3 build.

## The archived repository

The earlier repository is archived and kept private with its full history: all branches,
the archive tags `archive/explore-2026-10` (pipeline audit and parquet
review artefacts) and `archive/taxonomy-v0.9-2026-10` (the v0.9.1 catalogue branch with the
v0.8→v0.9.1 crosswalk and changelogs), the v1 pipeline and corpus database, the demos, the
v3 specification history, taxman, Dan Elton's experiments and dashboards, and the evaluation
material removed before publication.

Commit ids cited in `docs/DECISIONS.md` and in data and run records refer to commits of that archived repository;
they do not resolve in this one.
