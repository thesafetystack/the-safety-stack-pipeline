# The Safety Stack Data Repository

The metadata, taxonomy and schema repository of The Safety Stack project.

---

***The Safety Stack project.** Frontier labs' current alignment strategy is primarily defense-in-depth \- a stack of safety techniques at every layer from data collection to deployment. The number of safety techniques, published papers, AI models, expert opinions, etc. is vast and fast growing, and there is no place that collects this information and presents it in a comprehensive way \- the closest existing resources are periodic prose overviews at the resolution of research agendas. And more importantly, much of this information is unknown (e.g. performance in various contexts) or not public, and there is no "negative map" of what is unknown.*

**Our project's goal** is to map the space of contemporary AI safety techniques by collecting available information about the techniques, their efficiency, and deployment. The goal is not just an extensive collection of relevant literature but creating a comprehensive map and showing the gaps: techniques that have not been developed, have not been deployed, are not sufficiently effective in preventing harms, or have already demonstrably failed.

**The dataset** is hosted at [thesafetystack/the-safety-stack](https://huggingface.co/datasets/thesafetystack/the-safety-stack) (Hugging Face). Along with this repository, they form the growing data base of the project. The dataset is being extended and released on a weekly basis.

---

The catalogue takes each safety technique, gathers the relevant papers, evidence of its effectiveness and cost,
implementations, and what is known about its deployment across labs and models. The annotated metadata does not yet
cover all of these details; we are building the stack to infer what we can from available sources, including informal
ones (expert opinions, forecasts, compliance filings). The project builds on the
[Shallow Review of Technical AI Safety](https://shallowreview.ai/) and is meant to supply data for its 2026 edition.
It was in part funded via [Manifund](https://manifund.org/projects/the-ai-safety-stack).

## The dataset in brief

The dataset comprises public documents on technical AI safety (preprints and papers, LessWrong and Alignment
Forum posts, lab blog posts, system and model cards). Each record is annotated by a language model (`gpt-6-luna`; the
few documents it refused were annotated by a Claude model and are marked in the data) according to the techniques and
models taxonomies built by the project team (see [Taxonomies](#taxonomies)):

- the **techniques taxonomy** (234 AI-safety techniques): which techniques the document engages with and in what role
  (uses, evaluates, proposes, critiques, reports a lab using it);
- the **models catalogue** (1,877 AI model families, groups and models): which AI models it names and in what role.

Further annotations give the document class and, for papers with a full text (PDF, arXiv HTML or publisher XML), the
authors' affiliations and the organisations they name (the organisations vocabulary, a draft). The taxonomy files
and per-field provenance (the producing call of every annotation, and the overrides) ship with each release on
Hugging Face; the versions and their changelogs are here, in [taxonomies/](taxonomies/).

As of this release, it is not the complete catalogue as we imagine it. It does not, for instance, include
per-technique summaries from our taxonomy of techniques or a catalogue of evidence of use and deployment by frontier
labs yet. There are likely relevant documents missing and some irrelevant ones included; counts by year or by
technique describe this corpus, not the field. Annotations are model driven and may contain mistakes. Though the data
have not been individually verified, they have gone through an override process, and samples of them through an
[Annotation quality](https://huggingface.co/datasets/thesafetystack/the-safety-stack#annotation-quality) evaluation
against three language-model judges; the card describes both, and each override row's `method` says how it was
prepared. Inclusion is not endorsement, and nothing here is a verdict on a lab or a technique.

## What is where

| Where | What |
|---|---|
| This repository (`thesafetystack/the-safety-stack` on GitHub) | the taxonomies (exact files of every version, schemas, changelogs); the release formats ([formats/](formats/): the schemas of every format version, its changelog and compatibility rules); the taxonomy validator and the release checker ([tools/](tools/)); issue forms for corrections, takedowns and suggestions |
| [Hugging Face, `thesafetystack/the-safety-stack`](https://huggingface.co/datasets/thesafetystack/the-safety-stack) | the dataset: the data file, the dataset card with the numbers and the annotation quality figures, the format and vocabulary documentation, the taxonomy files used, the overrides applied, the manifest, the schema of each file and the release checker; one tagged revision per release |
| [`thesafetystack/the-safety-stack-pipeline`](https://github.com/thesafetystack/the-safety-stack-pipeline) on GitHub | the pipeline that builds the dataset: collection, screening, annotation, release |
| [thesafetystack.ai](https://thesafetystack.ai) | the project site |

The pipeline carries this repository as a git subtree (`data-repo/`) and reads the taxonomy files from it, each
pinned by tag and sha256. Taxonomy changes are made here and then pulled into the pipeline.

The published dataset contains no document texts and, for now, no abstracts; the annotations carry short quoted
passages as evidence. An internal variant with abstracts and full texts exists for the maintainers and is not
distributed.

## Taxonomies

Three vocabularies, in [taxonomies/](taxonomies/):

- [techniques](taxonomies/techniques/): 234 AI-safety techniques (137 intervention, 32 monitoring, 65 assessment)
  and 9 group nodes (for generic mentions of a field). Current version 1.1.0 (1.0.0's content in the shared file
  structure).
- [models](taxonomies/models/): 1,877 AI model families, groups and models, with 134 makers. Current version 1.1.0:
  makers and labs are references to the organisations vocabulary.
- [orgs](taxonomies/orgs/): 4,841 organisations named in author affiliations, and the models' makers. Current
  version 0.2.2, a draft (0.x: ids may still change); models 1.1.0 refers to 0.2.0 (0.2.2 keeps every 0.2.0 id).

Each version is a separate file, `<name>-<major>.<minor>.<patch>.yaml`, tagged with its file name without `.yaml`
(`techniques-1.1.0`, `models-1.1.0`). A released file is never edited; any change is a new file with a CHANGELOG
entry. From 1.1.0 (orgs 0.2.0) on, the files share one structure, the catalogue envelope
([taxonomies/README.md](taxonomies/README.md)). From 1.0.0 on:

- Slugs (techniques) and node ids (models) are frozen within a major version: none is removed, merged, split or
  given a different meaning. A minor version may add new ones.
- Definition wording may be clarified in a minor or patch version without changing what it means.
- The definitions (techniques) and glosses (models) in a released file are the text the annotation model was shown
  when annotating with that version.

The earlier files (techniques 0.9.1, models 0.9.0, orgs 0.2.0) stay for reference; the 0.x files, the current orgs
0.2.2 included, carry no such guarantee.
Check a file with `uv run tools/taxonomy-schema-validator.py --all`; CI runs it on every push and pull request.

## Using the data

The [dataset card](https://huggingface.co/datasets/thesafetystack/the-safety-stack) describes the files and how to
load them, and carries the current release's numbers and figures. The data is held as one parquet file, one row per
document with every annotation nested in it, which DuckDB reads straight from the hub:

```python
import duckdb
con = duckdb.connect()
con.execute("CREATE VIEW d AS SELECT * FROM 'hf://datasets/thesafetystack/the-safety-stack/data/corpus.parquet'")
# documents whose own work uses RLHF (role strength 2 or more of 4: at least "minor but real")
con.sql("""
    SELECT doc_id, title, published_date, t.roles.used AS strength
    FROM d, unnest(annotations.safety_techniques.payload.techniques) AS u(t)
    WHERE t.slug = 'rlhf' AND t.roles.used >= 2
    ORDER BY published_date DESC
""").show()
```

You can also load the dataset via `polars` and Hugging Face `datasets`; the card has examples. The format and
vocabulary are documented in the dataset's files: `FORMAT.md` (files, columns, ids, recipes) and `VOCABULARY.md`
(every value word with a sentence).

### What is included

A document is included when it is publicly available and a language model screening its text judged it relevant to
at least one of two questions:

- **Does the document carry something about a concrete technique for making a general-purpose AI system or agent
  safer, more honest, more controllable, more transparent or harder to misuse, or for measuring any of these?** This
  may include the technique itself (at any maturity); evidence about it (results, failures, replications, negative
  results); evidence that an organisation uses, used or declined it; critique or expert judgement of it; or how
  techniques relate (surveys, taxonomies). A policy document counts only when it names, requires or assesses a
  concrete safeguard.
- **Is the document technical AI safety research in the wider theoretical sense, judged by topic and intent?** This
  was informed by the Shallow Review of Technical AI Safety, a yearly map of the field (work meant to prevent future
  AI systems from having large unintended negative effects). Beyond techniques this covers agent foundations and
  decision theory aimed at AI, model psychology, multi-agent and cooperative AI theory, questions of what AI should
  be aligned to, critiques of the field, and safety and dangerous-capability evaluations. This question excludes
  misuse and access controls (concrete misuse safeguards count under the first question), conventional security, and
  policy without a technical proposal.

The screening judges are tasked to focus on subject matter rather than quality. An informal, unreviewed or
self-interested source is included if it is on topic. Documents the screening process marks as borderline overall
(relevant to neither question) are not included. Out of scope are documents that use the same words in another
field's sense ("alignment" of sequences, "safety" of a drug, "control" of a plant), capability work with no safety
component, and general machine learning.

Candidates come from keyword and author searches of scholarly indexes (OpenAlex, arXiv, Semantic Scholar), LessWrong
and the AI Alignment Forum, blogs of frontier labs and AI-safety organisations, labs' model and system card pages, a
few newsletters, and select sources added by hand. The paper searches cover 2015 to the present; earlier documents
come mostly from the forums, organisations' blog archives and works added by hand. Apart from sources taken by rule
(posts of frontier labs, AI-safety organisations and newsletters, sources added by hand or linked from them), a
triage model first judges each record from its title and metadata to decide which full texts to fetch; the screen
then reads the texts. The screen is a model judgement
with known errors in both directions (see the card's "Annotation quality" section). If a work is missing or should not
be here, see [Corrections](#corrections-takedowns-and-missing-works).

## Releases

Dataset releases are dated (`YYYY-MM-DD`) and planned weekly. Each is a revision of the Hugging Face dataset tagged
`release-<date>`, with its documentation and its manifest, `release.json`, which records the build, row counts, the
taxonomy versions its annotations use (each with the sha256 of its file, the same file as in this repository), and
every file with its size and sha256. This repository changes only when the release format or a taxonomy gets a new
version, not with each release. There is no DOI per release; a Zenodo record for the dataset series is planned.

From format 1.0.0 on, each release names its format version (`format_version` in `release.json`, in the parquet
file's metadata and in the card), and ships the schema of each of its files beside it and the release checker
(`tools/release-validator.py`). The formats, what each version guarantees and how a new one is cut are in
[formats/](formats/README.md). To check a downloaded release:

```bash
uv run tools/release-validator.py <release directory> --rows all
```

Wherever it runs, the checker compares the release's schemas with the archived format (it carries their digests);
run from a checkout of this repository, it compares them with the archived files here and also checks the release's
taxonomy files against their schemas.

## Licences

| What | Licence |
|---|---|
| Taxonomies, schemas and documentation in this repository (everything outside `tools/`) | [CC0 1.0](LICENSE) |
| What we made in the dataset: the annotations, the identity and provenance structure, the release metadata, the prompts and documentation | CC0 1.0 |
| Bibliographic records in the dataset (titles, authors, dates, identifiers; abstracts where a release includes them) | as their providers supply them, under those providers' terms |
| Quoted document text in the dataset (evidence quotes) | its owners'; not covered by CC0 |
| Code in `tools/` | [MIT](tools/LICENSE) |

CC0 means no conditions; a citation is still appreciated.

Before the first dataset release this repository stated CC BY 4.0; the commits tagged `techniques-0.9.1`,
`techniques-1.0.0`, `models-0.9.0` and `models-1.0.0` still carry that LICENSE file. The same taxonomy
files are now CC0 1.0.

## Corrections, takedowns and missing works

- **Takedowns and other private requests:** email [info@thesafetystack.ai](mailto:info@thesafetystack.ai), for
  example to have a document, or your name, removed.
- **Public reports:** use the issue forms on this repository
  ([new issue](https://github.com/thesafetystack/the-safety-stack/issues/new/choose)):
  - **Correction**: an annotation or a metadata field is wrong.
  - **Missing work**: a document that fits the criterion above is not in the dataset.
  - **Taxonomy suggestion**: a missing technique or model, a wrong definition, a merge or split.
  - **Takedown**, if you are fine asking in public.

We acknowledge every report; we do not promise a date for resolving it. Accepted corrections and takedowns are
applied in the next release as overrides, which ship with the release on Hugging Face (`config/overrides.jsonl`). See
[CONTRIBUTING.md](CONTRIBUTING.md).

## Team

Tomáš Gavenčiak (lead; Alignment of Complex Systems group, Charles University, Prague), Jacob Livingston Slosser
(Pioneer Centre for AI, University of Copenhagen; CheatSheet; Juriscription), Dan Elton (Metascience Observatory),
Gavin Leech (advisor).

## Citation

For the dataset: The Safety Stack dataset, release `<date>`,
https://huggingface.co/datasets/thesafetystack/the-safety-stack (the release you used: its `release_id`, also the tag
`release-<date>`; the card gives the exact line).

For the taxonomies: [CITATION.cff](CITATION.cff) (GitHub shows it as "Cite this repository"; version 1.1.0 of the
techniques and models taxonomies).
