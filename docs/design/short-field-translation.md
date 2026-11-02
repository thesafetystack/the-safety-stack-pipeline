# Short-field translation (design note; implemented as option B, D755)

Status: **implemented as option B** (D755, decided 2026-10-30 after the proposal of D750): the stage
`annotate.translation` (`workflows/annotate/translation/`), payload `translation` {from_lang, title_en, abstract_en,
method}, role `translate` (gpt-6-luna; `model: openai/gpt-5.6-terra` documented as the stronger option), in plan
batch-2026-10a revision 9 after the screen, and the release kind `translation` (abstract_en follows the abstract
policy). Affiliations are not translated: the organisation vocabulary has Chinese aliases instead (D756). Sections 2-5
below are the proposal as written; where the implementation differs, it says so here: the payload is an annotation
record (not a `metadata.translated` struct: the `metadata` block is unchanged), and its model and prompt are in
the record's metadata rather than in the payload.

## 1. What is known

- **Full texts need no translation.** The Chinese-coverage review (its round 2) ran the screen and the
  technique prompts on four Chinese documents in Chinese and in an Opus translation: screen verdicts identical on 4
  documents x 2 models; technique slugs zh~en Jaccard 0.60-0.83 against 0.57-0.80 between two models on the same
  text; 75 of 76 quotes from the Chinese runs anchor in the original (the miss was a converter space, fixed in
  `stack/anchor.py`, D742). So translation concerns the short fields only.
- **The cuts no longer need it for Chinese.** `stack/rules.py` matches Chinese AI terms and safety stems directly
  (D744), so a Chinese title or abstract passes the `ai_term` cut and the arXiv category filter on its own words.
- **Identity matching within one language works** (`norm_title` keeps CJK, Cyrillic, Hangul, D744). What a
  translation adds is matching *across* languages: a Chinese journal record and the English arXiv preprint of the
  same work. No such pair is measured yet; the corpus has 22 documents of 39,269 whose screen `primary_lang` is not
  English (the Chinese-coverage review), and `china_coverage.yaml` adds 5 Chinese-only documents and one bilingual one
  (9 before its four WeChat articles were dropped, 2026-10-30).
- **The organisation vocabulary** matches affiliation strings, not titles; its aliases are Latin-only. Chinese
  affiliation strings would need translated affiliations (or Chinese aliases), a different field from title and
  abstract.

## 2. Field shape

On a document's `metadata` (and the release row), beside the originals, never replacing them:

```yaml
title: "人工智能安全治理框架 2.0版"          # as the source gave it
abstract: "…"                              # as the source gave it
translated:                                # NULL when nothing is translated (English, or not asked)
  title_en: "AI Safety Governance Framework 2.0"
  abstract_en: "…"                          # NULL when there is no abstract
  from_lang: "zh"                           # ISO 639-1 of the original, as detected
  method: "llm"                             # `llm` | `human` (a `fields` override) | `source` (an English
                                            #   title the source itself gave, e.g. a bilingual record)
  model: "openai/gpt-6-luna"                # the model id as pinned; NULL for human / source
  prompt: "translate-short-v1"              # the prompt name (its digest is in the stage's pin)
```

One struct rather than flat `title_en` / `abstract_en` columns, so the provenance travels with the text and a
release reader cannot use a translation without seeing that it is one. The release maps it to a nullable struct
column `translated` with the same fields; FORMAT.md says "machine translation, not the source's text". Public
variant: `title_en` ships; `abstract_en` follows the abstract policy (a translation of a withheld abstract is withheld:
`mapping.yaml` `abstract: withhold` applies to both).

## 3. Placement: where the call runs

| | A. in the collectors, before the cut | B. a stage after gather (`annotate.translate`) | C. B, plus gather reading the previous build's translations |
|---|---|---|---|
| serves the cuts | yes | no (D744 serves Chinese) | no (D744 serves Chinese) |
| serves cross-language identity | yes (the source row carries it) | no | yes, one build later |
| serves orgvocab | only if affiliations are translated too | same | same |
| LLM calls | per non-English **hit**, kept or not (most are cut); in every collector that can see one | per non-English **document** in scope (tens today) | as B |
| cache / evidence | the collector writes no evidence family; the call would need a ledger and cache in collect (collect.links has both, its `link_select` records) | an annotation with its evidence family, like every annotator: cached, pinned, recomputed on a prompt or model change | as B |
| format change | sources and documents `metadata` (strict parquet structs) gain `translated`: two family versions and upcasters | a payload in `stack/schemas/payloads/registry.py` and `annotations.other[]` (a family format change too), the release mapping | B, plus gather reading an annotation input (a new input of gather) |
| determinism | collect becomes model-dependent (a cut decision depends on a model answer) | collect and gather stay deterministic | gather depends on a prior build's annotation |

Recommendation: **B**, with `method: source` filled deterministically where a record already carries an English
title (OpenAlex and CNKI records often do), and A rejected (it makes cut decisions model-dependent and pays for hits
that are then cut). C only if cross-language duplicates turn out to exist in numbers; until then an `identity` merge
override handles a pair by hand.

## 4. What B would be, minimally

- Scope: documents in the corpus scope whose title or abstract has a non-Latin script share above a threshold
  (the `non_latin_ratio` measure of `stack/conversion/quality.py`), or whose screen `primary_lang` is not `en`.
- One call per document through `stack.llm` (the pipeline's client: budget, ledger, response cache, egress by licence
  class), a new role `translate_short` (gpt-6-luna; `restricted` and `api_tos` documents allowed to the role's
  provider as for the screen), JSON output `{title_en, abstract_en, from_lang}`, max 1,500 output tokens.
  Cost at today's volume (tens of documents, about 600 input and 400 output tokens each): well under $0.10 a full
  recompute.
- Evidence: the stage's evidence family records the request and answer like every annotator; the payload is the
  struct above without `method`/`model`/`prompt`, which the fold fills from the pin.
- Release: `fold.dataset` maps `annotations.translate.payload` to the `translated` column; a `fields` override with
  `method: human` wins over the model's (D666's field precedence).

## 5. Decisions (D750 asked, D755 and D756 decided)

1. Placement: B (recommended), A or C.
2. Field shape: the `translated` struct (recommended) or flat `title_en` / `abstract_en` columns.
3. Release: ship `title_en` in the public variant (recommended), keep `abstract_en` with the abstract policy.
4. Affiliations: translate them in the same call (an `affiliations_en` list feeding orgvocab), or add Chinese aliases
   to the organisation vocabulary instead (no model; the matcher's tokeniser would then need to keep CJK runs, a
   `MATCHER_VERSION` bump that recomputes every document's `affiliation_orgs`, no LLM).
5. Model and role: gpt-6-luna under a new role `translate_short`, or another model.
