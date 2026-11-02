# The Safety Stack dataset, release {{release_id}}

{{n_docs}} public documents on technical AI safety, published {{year_min}}-{{year_max}}, {{recent_share}} of them from
{{recent_from}} on: {{type_summary}}. A language model annotated each with the safety techniques of our taxonomy it
engages with ({{n_techniques}} techniques, plus {{n_groups}} group nodes for generic mentions of a field) and in what
role (uses, evaluates, proposes, critiques, reports a lab using it); the AI models of our catalogue it names
({{n_models}} models, model families and groups) and in what role; its document class; and, for papers with a full
text, its authors' affiliations{{orgs_clause}}. The taxonomies and per-field provenance (the producing call of every annotation,
and the overrides) ship with the data. The dataset is a deliverable of [The Safety Stack](https://thesafetystack.ai) project
([project page](https://manifund.org/projects/the-ai-safety-stack)) and builds on the Shallow Review of Technical AI
Safety, a yearly map of technical AI safety research by research agenda.

The paper searches start in {{early_before}}; the {{early_total}} earlier documents are {{early_forum}} forum posts
(LessWrong, Alignment Forum), {{early_blog}} posts from organisations' blog archives, {{early_manual}} works added by
hand or linked from hand-added pages, and {{early_other}} others.

A document is included when a language model screening its text judged it relevant to at least one of two
questions: does it carry something about a concrete technique for making general-purpose AI systems safer (the Stack
question, `annotations.stack_relevance`), and is it technical AI safety research in the Shallow Review's sense
(`annotations.shallow_review_relevance`); borderline documents are left out. {{q_both}} documents are relevant to
both questions, {{q_s_only}} only to the Stack question and {{q_r_only}} only to the Shallow Review question (the other
answer irrelevant or borderline){{q_neither}}; filter on the question you care about. Candidates come from keyword and author
searches of scholarly indexes (OpenAlex, arXiv, Semantic Scholar), LessWrong and the Alignment Forum, the blogs of
frontier labs and AI-safety organisations, model and system card pages, a few newsletters and sources added by hand.
A triage model judged each record from its title and metadata to decide which full texts to fetch; the screen then
read the texts (of {{rows_in}} documents with text in this build, {{n_docs}} were screened relevant).

It is not the technique catalogue the project is building: there are no per-technique summaries of evidence and no deployment status yet. It is not a
census: the searches, the triage and the relevance screen each miss relevant documents, and the screen lets some
irrelevant ones in, so counts by year or by technique describe this corpus, not the field. The annotations are
model-made and not systematically reviewed by people; they record what a document says, not whether it is true.
{{overrides_summary}} Inclusion is not endorsement, and nothing here is a verdict on a lab or a technique.

How well the annotations agree with three language-model judges: [Annotation quality](#annotation-quality) below.
For {{standin_docs}} documents whose request the main model refused, the annotation is a stand-in by a Claude model
(`annotator`, `provenance = override`); those were not evaluated.

**Quick start.** The dataset is one parquet file, `data/corpus.parquet`: one row per document, with every annotation
nested in it. DuckDB and polars read it straight from the hub. While the repository is private you need a Hugging Face
access token with read access to it (<https://huggingface.co/settings/tokens>) in the environment variable `HF_TOKEN`.

DuckDB (SQL; `unnest` turns a document's list of techniques into one row per technique):

```python
import duckdb
con = duckdb.connect()
con.execute("CREATE SECRET hf (TYPE huggingface, PROVIDER credential_chain)")  # reads HF_TOKEN; while private
con.execute("CREATE VIEW d AS SELECT * FROM 'hf://datasets/{{hf_repo}}/data/corpus.parquet'")
# documents whose own work uses RLHF (role strength 2 or more of 4: at least "minor but real")
con.sql("""
    SELECT doc_id, title, published_date, t.roles.used AS strength
    FROM d, unnest(annotations.safety_techniques.payload.techniques) AS u(t)
    WHERE t.slug = 'rlhf' AND t.roles.used >= 2
    ORDER BY published_date DESC
""").show()
```

polars (reads `HF_TOKEN` from the environment):

```python
import polars as pl
d = pl.scan_parquet("hf://datasets/{{hf_repo}}/data/corpus.parquet")
techniques = (d.select("doc_id", "title", t=pl.col("annotations").struct.field("safety_techniques")
                                         .struct.field("payload").struct.field("techniques"))
              .explode("t").unnest("t"))   # one row per (document, technique)
rlhf = techniques.filter((pl.col("slug") == "rlhf") & (pl.col("roles").struct.field("used") >= 2)).collect()
```

Hugging Face `datasets` (also uses `HF_TOKEN`; nested fields arrive as Python dicts and lists):

```python
from datasets import load_dataset
docs = load_dataset("{{hf_repo}}", split="train")
```

Each query on the `hf://` URL reads the remote file again; for repeated work download it once, with
`hf download {{hf_repo}} --repo-type dataset --revision release-{{release_id}} --local-dir stack` (the whole
release, {{n_files}} files) and read `stack/data/corpus.parquet`. To pin this release when reading from the hub, use its tag:
`hf://datasets/{{hf_repo}}@release-{{release_id}}/data/corpus.parquet`, or `revision="release-{{release_id}}"` in
`load_dataset`. For AI models, note that `annotations.model_roles.payload.models[]` lists every catalogue model the
name matcher found, rejected candidates with every role 0 included: count a model only where `roles.mentioned >= 1`
(FORMAT.md has the recipe). The snippets were tested with DuckDB 2.0, polars 2.1 and `datasets` 5.1 (`pip install duckdb polars
datasets`; the `hf` command comes with `huggingface_hub`).

Every field is documented in [FORMAT.md](FORMAT.md), with more DuckDB, polars and pandas recipes, and every value
word in [VOCABULARY.md](VOCABULARY.md).

**Project, licence, corrections.** Site: <https://thesafetystack.ai>. Taxonomies:
<https://github.com/thesafetystack/the-safety-stack>; the overrides applied to this release ship with it, in
`config/overrides.jsonl`. Corrections, missing works and takedowns: the
[issue forms](https://github.com/thesafetystack/the-safety-stack/issues/new/choose), or privately info@thesafetystack.ai; we
acknowledge every request. Licence: CC0 1.0 for what we made (the annotations, the identity and provenance
structure, the release metadata, the taxonomies, prompts and documentation); bibliographic records are as their providers supply them,
under those providers' terms, and quoted document text is its owners' (see `LICENSE`). Releases are dated and
planned weekly; each is a revision of this repository tagged `release-<date>`.

{{variant_note}}

Team: Tomáš Gavenčiak (lead; Alignment of Complex Systems group, Charles University, Prague), Jacob
Livingston Slosser (Pioneer Centre for AI, University of Copenhagen; CheatSheet; Juriscription), Dan Elton
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
