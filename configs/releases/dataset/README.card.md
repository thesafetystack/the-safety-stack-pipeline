# The Safety Stack dataset, release {{release_id}}

The annotated document index of [The Safety Stack](https://thesafetystack.ai) project, with taxonomies of safety
techniques and AI models in the [data repository](https://github.com/thesafetystack/the-safety-stack).

**The Safety Stack project.** Frontier labs' current alignment strategy is primarily defense-in-depth — a stack of
safety techniques at every layer from data collection to deployment. The number of techniques, papers, AI models and
expert opinions is vast and fast growing. Much of this information is unknown or not public, and there is no
"negative map" of what is unknown.

**Our project's goal** is to map the space of contemporary AI safety techniques by collecting available information
about the techniques, their efficiency and deployment. The goal is not just an extensive collection of relevant
literature but creating a comprehensive map and showing the gaps: techniques that have not been developed, have not
been deployed, are not sufficiently effective in preventing harms, or have already demonstrably failed.

**This release in numbers.**

- {{n_docs}} public documents on technical AI safety: {{type_summary}}.
- Published {{year_min}}-{{year_max}}; {{recent_share}} of them from {{recent_from}} on.
- Collected from {{n_channels}} source channels: scholarly indexes (OpenAlex, arXiv, Semantic Scholar), LessWrong
  and the Alignment Forum, the blogs of frontier labs and AI-safety organisations, model and system card pages, a few
  newsletters and sources added by hand. The paper searches cover {{early_before}} to the present.
- Selection: {{triage_not_relevant}} records marked not relevant by triage from title and metadata (their text was
  not fetched); of the {{rows_in}} documents whose text was read by the relevance screen, {{n_docs}} were judged
  relevant and shipped, {{screened_out}} were not.
- Annotated against {{n_techniques}} safety techniques (plus {{n_groups}} group nodes for generic mentions of a field),
  {{n_models}} AI models, model families and groups, and {{n_orgs}} organisations.

We are providing an early release to make the annotated document index available so researchers can use the data and
report errors while the annotations and taxonomies are being reviewed and deployment status is being investigated.
The dataset builds on the [Shallow Review of Technical AI Safety](https://shallowreview.ai/), a yearly map of
technical AI safety research by research agenda, and is meant to supply data for its 2026 edition.

**What is included.** A document is included when a language model screening its text judged it relevant to at least
one of two questions: does it carry something about a concrete technique for making general-purpose AI systems safer
(the Stack question, `annotations.stack_relevance`), or is it technical AI safety research in the Shallow Review's
sense (`annotations.shallow_review_relevance`); documents borderline overall (relevant to neither question) are left
out. {{q_both}} documents are relevant to both questions, {{q_s_only}} only to the Stack question and {{q_r_only}}
only to the Shallow Review question (the other answer irrelevant or borderline){{q_neither}}; filter on the question
you care about. Apart from sources taken by rule (posts of frontier labs, AI-safety organisations and newsletters,
sources added by hand or linked from them), a triage model judged each record from its title and metadata to decide
which full texts to fetch; the screen then read the texts. The full criteria are in the data repository's
[README](https://github.com/thesafetystack/the-safety-stack#what-is-included).

**Notes on the coverage.** As of this release, it is not a complete catalogue. It does not, for instance, include
per-technique summaries from our taxonomy of techniques or a catalogue of evidence of use and deployment by frontier
labs. There are likely relevant documents missing and some irrelevant documents included: the searches, triage and
relevance screen each miss relevant documents, and the screen lets some irrelevant ones in. Counts by year or by
technique describe this corpus, not the field. The {{early_total}} documents from before {{early_before}}, when the
paper searches start, are {{early_forum}} forum posts (LessWrong, Alignment Forum), {{early_blog}} posts from
organisations' blog archives, {{early_manual}} works added by hand or linked from hand-added pages, and
{{early_other}} others.

**Annotation and metadata.** The processing of the dataset has been model-driven (`gpt-6-luna`). The annotation of
the data records the model's reading of a document: which safety techniques from our taxonomy it discusses and in
what role (uses, evaluates, proposes, critiques, reports a lab using it); which AI models from our catalogue it names
and in what role; its document class; and, for papers with a full text, its authors' affiliations{{orgs_clause}}.
These readings record what a document says, not whether it is true. The taxonomies and per-field provenance (the
producing call of every annotation, and the overrides) ship with the data; their versions and changelogs are in the
data repository.

These readings have not been individually verified, but have gone through an
[Annotation quality](#annotation-quality) pipeline; the section below gives their agreement with three language-model
judges. {{overrides_summary}} Including a document in the dataset does not mean the maintainers endorse it, and
nothing in the dataset is a judgement about a lab or a technique.

Documents whose annotation came from a Claude model because the main model refused them are marked in the data
(`annotator`, `provenance = override`; {{standin_docs}} documents in this release). They are not in the judge samples
of the Annotation quality section.

**Quick start.** The dataset is one parquet file, `data/corpus.parquet`: one row per document, with every annotation
nested in it. DuckDB and polars read it straight from the hub. Reading a private repository needs a Hugging Face
access token with read access to it in the environment variable `HF_TOKEN` (and, in DuckDB, the commented line below).

DuckDB (SQL; `unnest` turns a document's list of techniques into one row per technique):

```python
import duckdb
con = duckdb.connect()
# con.execute("CREATE SECRET hf (TYPE huggingface, PROVIDER credential_chain)")  # private repositories: reads HF_TOKEN
con.execute("CREATE VIEW d AS SELECT * FROM 'hf://datasets/{{hf_repo}}/data/corpus.parquet'")
# documents whose own work uses RLHF (role strength 2 or more of 4: at least "minor but real")
con.sql("""
    SELECT doc_id, title, published_date, t.roles.used AS strength
    FROM d, unnest(annotations.safety_techniques.payload.techniques) AS u(t)
    WHERE t.slug = 'rlhf' AND t.roles.used >= 2
    ORDER BY published_date DESC
""").show()
```

polars:

```python
import polars as pl
d = pl.scan_parquet("hf://datasets/{{hf_repo}}/data/corpus.parquet")
techniques = (d.select("doc_id", "title", t=pl.col("annotations").struct.field("safety_techniques")
                                         .struct.field("payload").struct.field("techniques"))
              .explode("t").unnest("t"))   # one row per (document, technique)
rlhf = techniques.filter((pl.col("slug") == "rlhf") & (pl.col("roles").struct.field("used") >= 2)).collect()
```

Hugging Face `datasets` (nested fields arrive as Python dicts and lists):

```python
from datasets import load_dataset
docs = load_dataset("{{hf_repo}}", split="train")

def used(row, slug):   # the document's own work uses the technique (strength 2 or more of 4: "minor but real")
    st = row["annotations"]["safety_techniques"]
    return any(t["slug"] == slug and t["roles"]["used"] >= 2 for t in (st and st["payload"]["techniques"]) or [])

rlhf = docs.filter(lambda r: used(r, "rlhf"))
```

Each query on the `hf://` URL reads the remote file again; for repeated work download it once, with
`hf download {{hf_repo}} --repo-type dataset --revision release-{{release_id}} --local-dir stack` (the whole
release, {{n_files}} files) and read `stack/data/corpus.parquet`. To pin this release when reading from the hub, use its tag:
`hf://datasets/{{hf_repo}}@release-{{release_id}}/data/corpus.parquet`, or `revision="release-{{release_id}}"` in
`load_dataset`. The snippets were tested with DuckDB 2.0, polars 2.1 and `datasets` 5.1 (`pip install duckdb polars
datasets`; the `hf` command comes with `huggingface_hub`).

**Model mentions.** `annotations.model_roles.payload.models[]` lists every catalogue model the name matcher found,
including the candidates the annotation rejected, which have every role 0: count a model only where
`roles.mentioned >= 1` (FORMAT.md has the recipe).

**Checking a download.** The release ships its checker: `uv run stack/tools/release-validator.py stack --rows all`
checks the format version (`{{format_version}}`), every file against `release.json` and against its schema (every
row), the ids and the taxonomy pins; exit code 0 = valid. FORMAT.md's "Format versions" says what a format version
guarantees.

Every field is documented in [FORMAT.md](FORMAT.md), with more DuckDB, polars and pandas recipes, and every value
word in [VOCABULARY.md](VOCABULARY.md).

**Project, licence, corrections.**

- Site: <https://thesafetystack.ai>; [project page](https://manifund.org/projects/the-ai-safety-stack).
- Taxonomies, their versions and changelogs, and release formats: the
  [data repository](https://github.com/thesafetystack/the-safety-stack). The overrides applied to this release are in
  `config/overrides.jsonl`.
- The public [pipeline](https://github.com/thesafetystack/the-safety-stack-pipeline) builds the dataset and includes
  its prompts and the judge evaluation.
- For errors, missing works or takedowns, use the GitHub
  [issue forms](https://github.com/thesafetystack/the-safety-stack/issues/new/choose) or write privately to
  info@thesafetystack.ai. We acknowledge every request.

What we made — the annotations, the identity and provenance structure, the release metadata, the taxonomies and the
documentation — is released under CC0 1.0, to the extent we hold copyright or database rights in it. Third-party
material, wherever it occurs in the release (titles and bibliographic records as their providers publish them, quoted
excerpts, verbatim surfaces), is not licensed by us and remains its owners'. {{quote_limits}} The release checker in
`tools/` is MIT-licensed, as in the data repository.
See `LICENSE`. Releases are dated and planned weekly; each is a revision of this repository tagged `release-<date>`.

**Personal data.** The rows carry authors' names and affiliations as the works and their indexes publish them, for
one purpose: a research index of technical AI-safety literature, in which each work is found and attributed by its
authors. {{email_note}} To have a record corrected, or to object to or ask for the removal of personal data about you,
write to info@thesafetystack.ai or use the issue forms above.

{{variant_note}}

Team: Tomáš Gavenčiak (lead; Alignment of Complex Systems group, Charles University, Prague), Jacob
Livingston Slosser (Sapien Institute; Pioneer Centre for AI, University of Copenhagen), Dan Elton
(Metascience Observatory), Gavin Leech (advisor).
Cite as: The Safety Stack dataset, release {{release_id}}, https://huggingface.co/datasets/{{hf_repo}}.

## Annotation quality

{{quality_section}}

## Coverage

{{coverage}}

## Files

{{files_table}}

## Release record

{{release_record}}

## Fields of `data/corpus.parquet` (top levels; FORMAT.md has every field)

{{field_table}}
