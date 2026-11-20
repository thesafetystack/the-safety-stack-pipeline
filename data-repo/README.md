# The Safety Stack Data Repository

The metadata, taxonomy and schema repository of The Safety Stack project.

---

*__The Safety Stack project.__ Frontier labs' current alignment strategy is primarily defense-in-depth - a stack of safety techniques at every layer from data collection to deployment. The number of safety techniques, published papers, AI models, expert opinions, etc. is vast and fast growing, and there is no place that collects this information and presents it in a comprehensive way - the closest existing resources are periodic prose overviews at the resolution of research agendas. And more importantly, much of this information is unknown (e.g. performance in various contexts) or not public, and there is no "negative map" of what is unknown.*

**Our project's goal** is to map the space of contemporary AI safety techniques by collecting available information about the techniques, their efficiency, and deployment. The goal is not just an extensive collection of relevant literature but creating a comprehensive map and showing the gaps: techniques that have not been developed, have not been deployed, are not sufficiently effective in preventing harms, or have already demonstrably failed.

**The dataset** is hosted at [thesafetystack/the-safety-stack](https://huggingface.co/datasets/thesafetystack/the-safety-stack) (Hugging Face). Along with this repository, they form the growing data base of the project. The dataset is being extended and released on a weekly basis.

The dataset is the first deliverable of the larger product we are building, a catalogue of AI safety techniques. The catalogue takes each safety technique, gathers the relevant papers, evidence of its effectiveness and cost, implementations, and what is known about its deployment across labs and models. While at the moment the annotated metadata does not cover all of these details we are building the stack to infer what we can from available sources including informal sources (expert opinions, forecasts, compliance filings). The project builds on the [Shallow Review of Technical AI Safety](https://shallowreview.ai/) and is meant to supply data for its 2026 edition. The project was in part funded via [Manifund](https://manifund.org/projects/the-ai-safety-stack).

- **For:** safety researchers getting oriented in the field; funders looking for neglected work and expert opinion; evaluators and policy analysts who need an overview of the stack and an index of sources.
- **Not for:** tracking individuals beyond paper authorship; judging or ranking labs' safety efforts; calls to action other than research recommendations.
- **Team:** Tomáš Gavenčiak (lead; Alignment of Complex Systems group, Charles University, Prague), Jacob Livingston Slosser (Sapien Institute; Pioneer Centre for AI, University of Copenhagen), Dan Elton (data; Metascience Observatory), Gavin Leech (advisor).

### The dataset in brief

The dataset is comprised of public documents on technical AI safety (preprints and papers, LessWrong and Alignment Forum posts, lab blog posts, system and model cards). Each record is annotated by a language model (gpt-6-luna) according to a techniques taxonomy built by the project team. Data points cover role (uses, evaluates, proposes, critiques, reports a lab using it), which AI models are used and in what role, its document class, and, for papers with a full text (PDF, arXiv HTML or publisher XML), its authors' affiliations and the organisations they name. The taxonomies can be found [here](taxonomies/); per-field provenance ships with each release on Hugging Face.

As of this release, it is not the complete catalogue as we imagine it. It does not, for instance, include per-technique summaries from our taxonomy of techniques, nor evidence of use or deployment status by frontier labs yet. Similarly, we estimate the coverage to be around 85% accurate: there are likely relevant documents missing, and some irrelevant ones included. Annotations are model driven and may contain mistakes. Though the data have not been individually verified, they have gone through an [Annotation quality](https://huggingface.co/datasets/thesafetystack/the-safety-stack#annotation-quality) pipeline, triage, sampling and override process. Each override row's `method` says how it was prepared. Inclusion is not endorsement, and nothing here is a verdict on a lab or a technique.

## What is where

| Where | What |
|---|---|
| This repository (`thesafetystack/the-safety-stack` on GitHub) | the taxonomies (exact files of every version, schemas, changelogs); the release formats ([formats/](formats/): the schemas of every format version, its changelog and compatibility rules); the taxonomy validator and the release checker ([tools/](tools/)); issue forms for corrections, takedowns and suggestions |
| [Hugging Face, `thesafetystack/the-safety-stack`](https://huggingface.co/datasets/thesafetystack/the-safety-stack) | the dataset: the data file, the dataset card with the annotation quality figures, the format and vocabulary documentation, the taxonomy files used, the overrides applied, the manifest, the schema of each file and the release checker; one tagged revision per release |
| [`thesafetystack/the-safety-stack-pipeline`](https://github.com/thesafetystack/the-safety-stack-pipeline) on GitHub | the pipeline that builds the dataset: collection, screening, annotation, release |
| [thesafetystack.ai](https://thesafetystack.ai/) | the project site |

The pipeline carries this repository as a git subtree (`data-repo/`) and reads the taxonomy files from it, each pinned by tag and sha256. Taxonomy changes are made here and then pulled into the pipeline.

The published dataset contains no document texts and, for now, no abstracts; the annotations carry short quoted passages as evidence, limited per document (the release's `FORMAT.md`, "Quotes"), and email addresses we detect are removed. An internal variant with abstracts and full texts exists for the maintainers and is not distributed.

## Using the data

The [dataset card](https://huggingface.co/datasets/thesafetystack/the-safety-stack) describes the files and how to load them. The data is held as a parquet file which DuckDB reads straight from the hub:

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

You can also load the dataset via `polars` and Hugging Face `datasets`.

The current release, its documentation and its figures can be found on Hugging Face:

- **Format and vocabulary:** `FORMAT.md` (files, columns, ids, recipes) and `VOCABULARY.md` (every value word with a sentence) in the [dataset's files](https://huggingface.co/datasets/thesafetystack/the-safety-stack).
- **Quality:** the card's "Annotation quality" section: how far the annotations agree with a three judge language-model panel, with intervals and caveats.

### What is included

A document is included when it is publicly available and a language model screening its text judged it relevant to at least one of two questions:

- **Does the document carry something about a concrete technique for making a general-purpose AI system or agent safer, more honest, more controllable, more transparent or harder to misuse, or for measuring any of these?** This may include considerations such as: the technique itself (at any maturity); evidence about it (results, failures, replications, negative results); evidence that an organisation uses, used or declined it; critique or expert judgement of it; or how techniques relate (surveys, taxonomies). A policy document counts only when it names, requires or assesses a concrete safeguard.
- **Is the document technical AI safety research in the wider theoretical sense, judged by topic and intent?** This was informed by looking at the previous effort of the Shallow Review of Technical AI Safety, a yearly map of the field (work meant to prevent future AI systems from having large unintended negative effects). Beyond techniques this covers agent foundations and decision theory aimed at AI, model psychology, multi-agent and cooperative AI theory, questions of what AI should be aligned to, critiques of the field, and safety and dangerous-capability evaluations. It excludes misuse and access controls, conventional security, and policy without a technical proposal.

The screen judges are tasked to focus on subject matter rather than quality. An informal, unreviewed or self-interested source is included if it is on topic. Documents the screening process marks as borderline are not included. Out of scope are documents that use the same words in another field's sense ("alignment" of sequences, "safety" of a drug, "control" of a plant), capability work with no safety component, and general machine learning.

Candidates come from a number of sources through keyword and author searches of scholarly indexes (OpenAlex, arXiv, Semantic Scholar), LessWrong and AI Alignment Forum, blogs of frontier labs and AI-safety organisations, labs' model and system card pages, a few newsletters, and select sources added by hand. The paper searches cover 2015 to 2026. The screening process is a model judgement with known errors in over and under inclusiveness (see the card's "Annotation quality" section). If a work is missing or should not be here, see [Corrections](#corrections-takedowns-and-missing-works).

## Taxonomies

Three vocabularies, in [taxonomies/](taxonomies/):

- [techniques](taxonomies/techniques/): separated into 234 AI-safety techniques (137 intervention, 32 monitoring, 65 assessment) and 9 group nodes. Current version 1.1.0 (1.0.0's content in the shared file structure).
- [models](taxonomies/models/): 1,877 AI model families, groups and models, with 134 makers. Current version 1.1.0: makers and labs are references to the organisations vocabulary.
- [orgs](taxonomies/orgs/): 4,841 organisations named in author affiliations, and the models' makers. Current version 0.2.2, a draft (0.x: ids may still change); models 1.1.0 refers to 0.2.0.

Each version is a separate file, `<name>-<major>.<minor>.<patch>.yaml`, tagged with its file name without `.yaml` (`techniques-1.1.0`, `models-1.1.0`). Any change is recorded as a new file with a CHANGELOG entry. From 1.1.0 (orgs 0.2.0) on, the files share one structure, the catalogue envelope ([taxonomies/](taxonomies/README.md)). From 1.0.0 on:

- Slugs (techniques) and node ids (models) are frozen within a major version: a minor version may add new ones.
- Definition wording may be clarified in a minor or patch version without changing what it means.
- The definitions (techniques) and glosses (models) in a released file are the text the annotation model was shown when annotating with that version.

The 0.x files (techniques 0.9.1, models 0.9.0, orgs 0.2.0 and 0.2.2) carry no such guarantee. Check a file with `uv run tools/taxonomy-schema-validator.py --all`; CI runs it on every push and pull request.

## Releases

Dataset releases are dated (`YYYY-MM-DD`) and planned weekly. Each is a revision of the Hugging Face dataset tagged `release-<date>`, with its documentation and its manifest, `release.json`, which records the build, row counts, the taxonomy versions its annotations use (each with the sha256 of its file, the same file as in this repository), and every file with its size and sha256. This repository changes only when the release format or a taxonomy gets a new version, not with each release. There is no DOI per release; a Zenodo record for the dataset series is planned.

From format 1.0.0 on, each release names its format version (`format_version` in `release.json`, in the parquet file's metadata and in the card), and ships the schema of each of its files beside it and the release checker (`tools/release-validator.py`). The formats, what each version guarantees and how a new one is cut are in [formats/](formats/README.md). To check a downloaded release:

```bash
uv run tools/release-validator.py <release directory> --rows all
```

Wherever it runs, the checker compares the release's schemas with the archived format (it carries their digests); run from a checkout of this repository, it compares them with the archived files here and also checks the release's taxonomy files against their schemas.

## Licences

| What | Licence |
|---|---|
| Taxonomies, schemas and documentation in this repository (everything outside `tools/`) | [CC0 1.0](LICENSE), to the extent we hold copyright or database rights in it |
| What we made in the dataset: the annotations, the identity and provenance structure, the release metadata, the taxonomies and documentation | CC0 1.0, to the extent we hold copyright or database rights in it |
| Third-party material wherever it occurs in the dataset (titles and bibliographic records as published, abstracts, quoted excerpts, verbatim surfaces) | not licensed by us; it remains its owners', under their terms |
| Code in `tools/`, and the release checker each release ships (`tools/release-validator.py`) | [MIT](tools/LICENSE) |

CC0 means no conditions; a citation is still appreciated.

Before release 1 this repository stated CC BY 4.0; the commits tagged `techniques-0.9.1`, `techniques-1.0.0`, `models-0.9.0` and `models-1.0.0` still carry that LICENSE file. The same taxonomy files are now CC0 1.0.

## Corrections, takedowns and missing works

- **Takedowns and other private requests:** email [info@thesafetystack.ai](mailto:info@thesafetystack.ai), for example to have a document, or your name, removed.
- **Public reports:** use the issue forms on this repository ([new issue](https://github.com/thesafetystack/the-safety-stack/issues/new/choose)):
  - **Correction**: an annotation or a metadata field is wrong.
  - **Missing work**: a document that fits the criterion above is not in the dataset.
  - **Taxonomy suggestion**: a missing technique or model, a wrong definition, a merge or split.
  - **Takedown**, if you are fine asking in public.

We acknowledge every report; we do not promise a date for resolving it. Accepted corrections and takedowns are applied in the next release as overrides, which ship with the release on Hugging Face (`config/overrides.jsonl`). See [CONTRIBUTING.md](CONTRIBUTING.md).

## Citation

For the dataset: The Safety Stack dataset, release `<date>`, https://huggingface.co/datasets/thesafetystack/the-safety-stack (the release you used: its `release_id`, also the tag `release-<date>`; the card gives the exact line).

For the taxonomies: [CITATION.cff](CITATION.cff) (GitHub shows it as "Cite this repository"; version 1.1.0 of the techniques and models taxonomies).
