# SR 2025 repository: screening and inclusion criteria as implemented

How the Shallow Review 2025 (SR) selected its items, as implemented in its public repository
<https://github.com/arb-consulting/shallow-review-2025> (its single commit `77efa9a` of 2025-12-17, read on
2026-10-12): an LLM pre-filter scored candidate links for AI-safety relevance and for inclusion, a threshold on the
scores produced a scaffold document, and the reviewers edited that document by hand into the final lists. Sections
1-5 give the stages, what the LLM stages were told, the thresholds, the exclusion rules, how agendas were assigned and
what of the data can serve as eval sets; section 6 compares them with `docs/scope/shallow-review.md` (written from
the website) and the rubrics `evals/rubrics/{triage,screen}.md`; Appendix A quotes the prompts. Where the
repository's own docs and its code differ, the code is described.

**Evidence labels.** [code] read in the repository's source; [data] computed from its data files (read-only queries
on `data.db`; normalised URLs, arXiv versions stripped); [text] stated in the review's own text
(`data/2025-12-16-draft-post-review/lw-post.md`, "Methods" section, line 2323 on); [inferred] a reading not stated
anywhere. Abbreviations: **ASR** = `ai_safety_relevance`, **SRI** = `shallow_review_inclusion` (the code and the
exports call it "sr"; it is not "Shallow Review" the project).

## 1. Pipeline as implemented

The headline: an LLM pre-filter produced two scores per candidate link; a threshold on them generated a scaffold
document; humans then edited the document by hand. **The final inclusion decision is human and unrecorded**: the
repository stores the LLM scores, a CSV of what the humans kept at 2025-11-09, and the final lists, but no per-item
reason for a human inclusion or exclusion.

### 1.1 Stages, files, counts

| # | Stage | Where | What happened | Counts |
|---|---|---|---|---|
| 1 | Candidate URL lists | `main-pipeline/data/sources-links/`, `data/sources-various_data/`, `scraping-scripts/` | URL lists from newsletters, author sweeps, forums, a paper list, manual dumps (section 1.2) | classify table: 3,384 rows, 3,280 distinct after normalisation (about 100 older rows are `www.`, trailing-slash or arXiv-version variants) [data] |
| 2 | `add` | `shallow_review/cli.py:375-545`, `add_items.py`, `prompts/add_items.yaml` | Each URL is typed by an LLM as `collection_source` (aggregator page, goes to `collect`) or `single_content` (goes to `classify`); when unsure it prefers `single_content`. `--phase` can bypass the LLM. | model `anthropic/claude-haiku-4-5`, thinking budget 1,024, max 2,000 tokens [code] |
| 3 | Scrape | `shallow_review/scrape.py`, `data/scraped/<sha256>.html.zst` | Browser scrape (playwright or selenium), cached forever, failures cached | 3,739 files; `scrape` table 3,930 rows, 164+1 failed [data] |
| 4 | Collect | `shallow_review/collect.py`, `prompts/collect.yaml` | LLM reads a stripped aggregator page and returns every link it thinks might be relevant, with a 0-1 `ai_safety_relevancy`; links at or above 0.3 are inserted into `classify` | 189 source pages (181 Zvi posts, 8 "dump" pages); 188 done; 1,817 links extracted, all at or above 0.3 by construction; 1,654 became classify rows [data] |
| 5 | Classify | `shallow_review/classify.py`, `prompts/classify.yaml` | LLM reads the stripped page (up to 100,000 tokens) with the taxonomy and returns metadata, two scores, `kind`, `contribution_type`, 1-3 category assignments | 3,384 rows: 3,138 done, 168 scrape_error, 48 classify_error, 30 new [data]. Run 2025-10-28 to 2025-12-16 |
| 6 | Scaffold export | `cli.py:858-1360` (`export-taxonomy`) | Items with SRI at or above 0.4, ASR at or above 0.1 and collect relevancy at or above 0.1 are grouped under their top category into a Markdown scaffold of the taxonomy | scaffold of 2025-10-28 says "Total items: 490"; its filters applied to the final DB give 386 [data] |
| 7 | Human pass | external Google Doc ("WIP doc"), then `SR2025-links-from-WIP-doc.csv` | Editors rewrote the scaffold by hand: dropped, added, moved links between agendas | CSV of the 2025-11-09 state: 663 rows, 611 unique URLs [data] |
| 8 | Feedback import | `shallow_review/feedback.py`, table `classify_feedback` | Doc links become `include` and `reclassify` feedback; exportable items missing from the doc become `exclude`; missing URLs are added to classify | 611 include, 603 reclassify (640 rows, 77 distinct categories), 154 exclude (158 rows), 149 note (152 rows) [data] |
| 9 | Re-export | `SR2025-reexport-after-11-09.md` | Scaffold regenerated respecting feedback | 699 unique papers in 736 placements; 48 included below thresholds by feedback; 120 re-classified because their LLM category no longer existed [text of file header] |
| 10 | Draft parse and enrich | `process_draft.py`, `shallow_review/draft_*.py`, `prompts/draft.yaml` | The edited review document is parsed (LLM extracts agenda attributes and output lists) and each output link is looked up in `classify` for title, authors, date; unknown links are added to classify for enrichment | draft links add 187 (`draft-2025-11-19`) and 133 (`draft_parser`) classify rows [data]. Model `claude-sonnet-4-5` [code, `draft_processing.py:185`]. Not an inclusion step |
| 11 | Final export | `data/2025-12-16-draft-post-review/` | `source.md` (the final document), `source-parsed.json/.yaml`, `agendas.csv`, `papers.csv`, `lw-post.md` | 81 agendas, 826 outputs (825 with a URL, 764 distinct normalised URLs), 6 output subsection headers [data] |

The counts disagree across the sources [text/data]: the README says 86 agendas and 827 outputs; `agendas.csv` has 79
rows (it omits the China and Others lab entries); the parsed JSON has 81 agendas plus 15 sections; `papers.csv` has
826 rows; the Methods text says "Of the 2000+ links ... in the raw scrape, about 700 made it in" and "3300 collected
candidate links".

### 1.2 Candidate collection: sources, queries, filters

| `classify.source` label | Rows | Origin | Done rows / of which in the final |
|---|---|---|---|
| `collected:zvi` | 1,561 | links extracted by the collect LLM from 181 Zvi Mowshowitz posts (`sources-links/zvi-posts-2025.txt`) | 1,356 / 72 |
| `arxiv-2025-papers-from-top-researchers` | 377 | arXiv papers Nov 2024 to Nov 2025 by SR2024's top researchers (`sources-various_data/SR2024-top-researchers.txt`, 110 names; `SR2024-some-names-ranked.csv`, 259 names with agenda and prominence 2.5-5), `sources-links/arxiv-2025-papers-from-top-researchers-in-SR2024(-filtered).txt` | 375 / 143 |
| `4_lw_curated` | 293 | LessWrong 2025 posts, see 1.3 (`scraping-scripts/AF-and-LW-scraper/results/4-lw_curated.csv`, 300 rows) | 276 / 84 |
| `papers-lenz` | 279 | Lenz's paper list (`sources-links/lenz-ai-alignment-paper-collection.txt`, 302 lines) | 274 / 100 |
| `arxiv-papers-from-gavin-authors`, `arxiv-papers-from-gavin-hits` | 94 + 92 | arXiv papers by author-name lists (`sources-various_data/{more,even-more}-names-from-gavin.txt`; `sources-links/arxiv_papers_*gavin_authors*.txt`) [mapping of the "hits" label to a file is inferred] | 93 / 16 and 92 / 10 |
| `arxiv-stephen` | 61 | an arXiv list, apparently from one of the repo authors [inferred]; no input file in the repo | 61 / 43 |
| `dump`, `collected:dump` | 57 + 93 | manual link dumps (`sources-links/dump-of-links-from-gavin-and-others.txt`, 146 lines) and the 8 aggregator pages among them | 56 / 16 and 93 / 32 |
| `draft-2025-11-19`, `draft_parser`, `feedback-SR2025-links-from-doc-11-09` | 187 + 133 + 157 | links read back out of the review's own draft (outputs, but also critiques, funders, lab pages) | 177 / 60, 129 / 117, 156 / 119 |

The three draft-derived sources are circular for evaluation (they were added because a human had put the link in
the document); the other nine are independent of the final decision. The last column counts `status = done` rows
whose normalised URL is in the final review [data].

Author sweeps: `misc_scripts/fetch_arxiv_papers.py` queries the arXiv API with `au:<lowercased last name> AND
submittedDate:[202411010000 TO 202511062359]` (last name only, so noisy); `misc_scripts/filter_papers_by_authors.py`
then keeps a paper only if an author matches a listed name (normalised full name, else last name plus first
initial, else last name when only that is listed). The `-filtered.txt` files are the output [code].

Not in the repo, although the review text claims them [text]: a Twitter/X scrape ("ad hoc Twitter for a year"; the
only trace is 133 x.com/t.co links, mostly extracted from Zvi posts [data]), the Gasteiger and Paleka newsletter
inputs (no list, no source label), conference-page inputs, and any LessWrong "AI" tag filter (section 1.3).

### 1.3 Side pipelines in `scraping-scripts/` (not the main database)

**LessWrong / Alignment Forum** (`AF-and-LW-scraper/`). `1-scrape-AF-or-LW.py` pulls every post of a year through the
site's GraphQL API ordered by newest, with no tag filter: 5,304 LW posts from 2025-01-01 to 2025-11-12 in
`results/1-lw_raw_posts.csv`. The query requests only id, title, URL, karma, comment count and date, so the `tags`,
`excerpt`, `author` and `wordCount` columns are empty in all 5,304 rows [data]. `2-analyze-AF-or-LW-links.py` then
keeps posts whose **title** (the only non-empty text field) contains one of: alignment, safety, evals,
interpretability, red teaming, governance, oversight, robustness, deception, capabilities, inner alignment, outer
alignment, rlhf, scaling, specification [code, lines 8-24, 41-46]; 637 pass. The top 300 by (karma, comment count)
are written as `4-lw_curated.csv` (cut-off karma 16). Of 129 raw LW posts that ended in the final review, this chain
kept 91 [data]. No LLM is involved. A separate sheet `results/sheets/all-alignment-forum-posts-2025-filtered.csv`
(438 AF posts, columns `title, link, karma, date, keep`; `keep` is `y` for 98, blank for 340) has unknown
provenance: only 27 of the 98 `y` posts are in the final review (48 of the 340 blank), so `keep` is not the final
decision. A notebook (`notebooks/AF-AI-post-filtering.ipynb`) shows an LLM filter over a 438-post AF list (the same size as
the sheet, probably the same posts [inferred]), in batches of 10 with the first 500 words of each post (prompt in
Appendix A.5; `gpt-5-mini`, reasoning effort `minimal`; the prompt asks to keep about 1 post in 10); its output file
is not in the repo.

**arXiv sweep** (`arxiv-scraper/`, six numbered scripts, `main.sh`). A late gap check against a draft, not the main
source of candidates [inferred from its inputs: a DOCX of the draft, 272 links, and Lenz's list]:

| Step | Script | Rule | Model | Result |
|---|---|---|---|---|
| 1 | `1-scrape.py` | default query `cat:cs.* AND (all:"ai safety" OR all:"ai alignment")`, submitted 2025-01-01 to 2025-12-31, limit 1,000 | none | 368 papers, published 2025-01-03 to 2025-10-23 |
| 2 | `2-filter_results.py` | tags plus 3-way relevance from title, authors, abstract, categories (Appendix A.3) | `gpt-4.1` (Responses API, structured output) | 159 "highly relevant", 183 moderately, 26 not |
| 4 | `4-find-unique-arxiv-links.py` | LLM duplicate check against the draft's links and Lenz's list (strict: mark not-new only on clear evidence) | `gpt-4.1` | 150 new, 9 duplicates |
| 5 | `5-curate-unique-arxiv-links.py` | relevance and quality bar, with the full draft text in the prompt (Appendix A.4) | `gpt-5-mini`, reasoning effort medium | 133 `y`, 17 `n` |
| 6 | `6-create-taxonomy-and-editorial-from-curated-links.py` | taxonomy and editorial draft from the 133 | `gpt-5-mini` | `results/7-taxonomy-and-editorial.txt` |

The text says the keyword set also includes "steerability" [text]; the code default has no such term (a `--query`
override could have been used; not verifiable). Final-review overlap [data]: 57 of the 159 "highly relevant" papers
are in the final review but only 8 of 183 "moderately relevant"; 49 of the 133 curated additions are in it, and 0
of the 17 rejected. File names in the README (`results/4-...`, `5-...`) do not match the code (`3-...`, `6-...`).

## 2. Relevance and inclusion criteria

### 2.1 What the LLM stages were told

Full text of the decisive prompt passages is in Appendix A (verbatim, with file:line). Summary of the **classify**
prompt (`prompts/classify.yaml`, 346 lines; the single most important file):

- *Definition* (`:5-7`): "work that intends to prevent very competent cognitive systems from having large
  unintended effects on the world". *Intent over method* (`:50`): if the work aims to understand or prevent
  unintended effects of advanced AI it is relevant "regardless of specific technical approach or framing".
- *Relevant work* (`:40-48`): technical alignment and safety research; interpretability; AI control, monitoring and
  evaluation methods; multi-agent safety and coordination; formal methods and provable safety; agent foundations
  and theoretical alignment; "governance-adjacent technical work (evals, standards, safety cases)"; "borderline
  technical topics offering novel causal perspectives on AI X-risk".
- *Two independent scores*, 0-1 (`:70-112`): **ASR** (topic relevance: 0.9-1.0 core, 0.7-0.9 clearly safety-focused,
  0.5-0.7 safety connections, 0.3-0.5 tangential, 0.1-0.3 mainstream ML, 0-0.1 none) and **SRI** (suitability: "Focus
  on technical depth and original contribution"). SRI anchors: 0.9-1.0 major original technical contribution;
  0.7-0.9 solid technical work, useful tools, substantive technical critiques; 0.5-0.7 foundational theory,
  preliminary results, technical position papers, tool releases; 0.3-0.5 limited direct contribution (high-level
  discussion, early-stage ideas, governance-focused analysis); 0.1-0.3 news, commentary, summaries without novel
  insight; 0-0.1 none. "Content can be highly relevant to AI safety but unsuitable for Shallow Review if it lacks
  technical depth." "Be conservative with inclusion scores" (`:341`).
- *Inclusion list* (`:85-96`): in = original technical research (empirical or theoretical, including foundational
  theory), technical agendas/roadmaps/frameworks, novel tools/benchmarks/datasets for safety research, substantive
  technical critiques. Out = news/commentary about research "(unless unclear if we have the original)", general
  capabilities work (agent frameworks, optimisation tools, coding benchmarks, formal verification of general code),
  governance-flavoured position papers without technical depth, opinion pieces, summaries/reviews without novel
  insight, mainstream AI "tangentially mentioning safety".
- *Pitfalls* (`:52-66`), with numeric rules: forecasting with statistical methodology gets SRI 0.50-0.75 (pure
  timelines speculation low); general capabilities work gets ASR and SRI below 0.4 ("long-horizon reasoning/planning
  is NOT automatically safety-relevant"; "Autonomy evals require BOTH autonomous agents AND safety relevance",
  `:334`); system cards are documentation, SRI below 0.4 "unless exceptionally detailed with novel methodology";
  preparedness frameworks, RSPs and technical agendas from major labs/orgs (OpenAI, Anthropic, OpenPhil, Redwood,
  MIRI) get SRI 0.70-0.85; overview/news summaries below 0.4, technical critiques above 0.7, position papers 0.5-0.75
  with technical depth and 0.3-0.5 when governance-flavoured; foundational theory 0.5-0.7 with moderate confidence.
- *Outputs* (`:114-142`): title, authors, organisations, date, year, venue, `kind` (17 values: paper_published,
  paper_preprint, blog_post, lesswrong, video, podcast, code_tool, dataset_benchmark, agenda_manifesto,
  news_announcement, social_media, course_educational, commercial, personal_page, blocked, error_detected, other),
  `contribution_type` (9 values), 1-2 sentence summary, key result, ASR, SRI, 1-3 category assignments with fit
  scores, a 3-4 sentence `category_comment` that must justify categories, ASR and SRI, and `confidence`
  ("NOT about work quality"; 0.5-0.7 for borderline or abstract-theory cases). Access failures: `kind="error_detected"`,
  `confidence=0.0`. Reasoning is requested before the JSON ("Quote key passages in your thinking").
- Six worked examples (`:162-301`) include the calibration points: alignment faking (ASR 0.98, SRI 0.95); "Against
  Almost Every Theory of Impact of Interpretability" (0.88, 0.75); OpenAI Preparedness Framework v2.0 (0.95, 0.78);
  a Claude system card (0.88, 0.38); the AI 2027 timelines forecast (0.85, 0.75); a goal-directedness workshop
  position paper (0.72, 0.55, confidence 0.55).

**Collect** prompt (`prompts/collect.yaml`) is wider and cheaper. Relevant topics (`:11-27`) add to the above:
robustness/adversarial examples/security, value and preference learning, reward modelling and RLHF, scalable
oversight/debate, ML-safety techniques (safe exploration, distribution shift), "AI forecasting and timelines (when
relevant to safety)", and "Borderline technical topics ... (e.g., Gradual Disempowerment, Multi-Agent Risk
scenarios)". Not relevant (`:31-38`): general ML/capabilities "unless it explicitly discusses safety implications",
domain applications, fairness/bias of current systems "unless framed as alignment/safety research", tech news,
promotional content, navigation/admin links, social-media commentary. Rules: **DATE FILTER** "Only collect material
from 2025 and from November-December 2024 ... If publication date is unclear ... be inclusive" (`:70`); "Be inclusive
rather than exclusive ... False positives are okay; we'll filter later" (`:71`); social media only for major
announcements by the authors themselves (`:74-79`); keep links scoring 0.3 or more (`:171`; the cut is enforced in code,
`collect.py:315`).

### 2.2 Models and parameters

| Call | Model | Parameters | Where |
|---|---|---|---|
| collect | `anthropic/claude-sonnet-4-5` (CLI default) | thinking budget 2,048 tokens, max_tokens 10,000, system prompt cached 1 h, temperature not set, timeout 120 s, up to 5 attempts, cache bypass by incrementing max_tokens after a parse error | `cli.py:551`, `collect.py:253-261`, `llm.py` |
| classify | `anthropic/claude-sonnet-4-5` (CLI default) | thinking budget 2,048, max_tokens 10,001, system prompt (incl. taxonomy) cached 1 h, HTML cap 100,000 tokens after stripping, results validated with pydantic and category IDs checked against taxonomy leaves | `cli.py:657`, `classify.py:293-303` |
| add (URL typing) | `anthropic/claude-haiku-4-5` | thinking budget 1,024, max_tokens 2,000, first 10,000 characters of stripped page | `add_items.py` |
| draft attribute extraction | `claude-sonnet-4-5` | structured JSON of agenda attributes and outputs | `draft_processing.py:185` |
| arXiv side pipeline | `gpt-4.1` (steps 2, 4), `gpt-5-mini` medium effort (5, 6) | structured outputs | `scraping-scripts/arxiv-scraper/*.py` |
| AF post filter (notebook) | `gpt-5-mini`, effort `minimal` | batches of 10 | notebook cell 3 |

No row in `data.db` records the model. `RunClassifyConfig` and `RunCollectConfig` in `common.py` default to
`claude-sonnet-4`, the CLI overrides them with `claude-sonnet-4-5`, and earlier runs may have used something else;
run logs and the LLM cache are gitignored, so the model per row cannot be verified [code]. The classify prompt's
`agent_prefill` key is never used by the code.

### 2.3 Thresholds that actually filtered

| Threshold | Value | Role | Source |
|---|---|---|---|
| collect relevancy to enter classify | 0.3 | recall-oriented; every extracted link was above it, nothing below was stored | `cli.py:550`, `collect.py:315` |
| `--min-relevancy` for classify | 0.0 | no filtering: every candidate was classified | `cli.py:656` |
| scaffold export | SRI at or above 0.4, ASR at or above 0.1, collect at or above 0.1 | what went to the human editors; the export command's own defaults are 0.1/0.1/0.1 | scaffold header of 2025-10-28; `cli.py:860-862` |
| "exportable" for inferring `exclude` | SRI at or above 0.4, ASR at or above 0.1 | an item above this that is absent from the human doc is recorded as excluded | `feedback.py:44, 206-207` |
| date | published on or after 2024-11-01 (full date), else year at or above 2024; undated items are kept | export-time only; the classify prompt has no date rule | `cli.py:1079-1096` |
| `kind` filter | none by default | `blocked`/`error_detected` rows are only removed by the SRI/ASR scores the LLM gave them | `cli.py:863` |
| confidence | not used for filtering | stored only | |

These thresholds were not calibrated against labels; they were set to give the editors a workable volume. By the
final DB, 1,181 of 3,138 classified rows have SRI at or above 0.4 [data].

## 3. Exclusion rules and borderline handling

1. **Rule-level exclusions** are exactly those in the prompts (2.1): news and commentary, general capabilities,
   governance without technical depth, opinion, summaries, system cards (SRI below 0.4), social media other than
   major author announcements, promotional or navigational links; and by code: nothing before 2024-11-01.
2. **Borderline handling.** The prompt has no borderline label. It asks for "moderate confidence (0.5-0.7)" on
   abstract foundational theory and borderline cases, and its worked borderline example (a workshop position paper,
   SRI 0.55, confidence 0.55) writes out a range ("Could range from 0.4 to 0.7"). The 0.4 SRI cut sends the whole
   0.4-0.7 band to the human editors; in practice the editors, not a second LLM pass, decide borderline items
   [inferred from the cut].
3. **Human exclusion is by omission.** `import_feedback_from_csv` writes `exclude` for every classified item with
   SRI at or above 0.4 and ASR at or above 0.1 that is not in the human document's CSV, with the note "Excluded:
   was exportable (sr>=0.4) but not in SR2025-links-from-WIP-doc.csv" (`feedback.py:249-260`). It records no human
   reason. `exclude` always wins in later exports (`cli.py:1009`). Of the 154 distinct excluded URLs, 46 are in the
   final review anyway (re-added later), so an omission at 2025-11-09 is weak evidence [data].
4. **Forced inclusion.** `include` feedback overrides the thresholds: 611 URLs, 48 of them below the SRI/ASR cut
   [data]. 478 of the 611 are in the final review.
5. **Re-classification after the taxonomy changed.** Items above the exportable cut whose top LLM category no longer
   existed were reset to `new` and re-classified once, then filtered by the normal thresholds: 152 items were reset
   (100 of them from `misc_*` categories; 41 from `misc_standards`), 120 reached the 2025-11-10 export
   [data, `feedback.py:221-247`; the 120 from the export header].
6. **Editorial exclusions outside the pipeline** [text]: agendas with no public outputs go to the bottom, inactive
   ones to a "Graveyard"; "an omission ... is not strong negative evidence"; social-network critiques generally
   excluded; "we mostly focus on papers and blogposts" (gdocs, tweets, GitHub, Discord not preferred). The final
   list is also narrower in kind than the pre-filter would suggest: among independent-source URLs, papers
   (`paper_preprint` 37 %, `lesswrong` 28 %, `paper_published` 18 % are in the final) dominate, `blog_post` is 9 %,
   `news_announcement` 2 %, and `social_media`, `podcast`, `video`, `commercial`, `personal_page`,
   `course_educational` are 0 % [data].

## 4. How agendas were assigned

1. **Taxonomy during classification.** The LLM chose 1-3 leaf IDs from a taxonomy file that began as 8 top-level
   groups and 103 leaves (2025-10-21, per `docs/DEVLOG.md`) and kept changing until the final version, formatted as
   hierarchical Markdown (about 5,800 tokens), the leaf descriptions drawing on the 2024 review. The taxonomy version
   used by each run is not recorded. Prompt text says "You must assign the content to ONE leaf category"
   while the JSON instructions ask for 1-3 (`taxonomy.py:194`, `classify.yaml:132-137`). 1,074 of 3,138 items (34 %)
   got `misc_other` as top category [data].
2. **Only the top category was used.** The scaffold export places each item under `categories[0]` only
   (`cli.py:1045-1046`); second and third categories and the fit scores were ignored.
3. **The human document took over.** Editors moved links between agendas in the WIP doc; `reclassify` feedback
   (603 URLs; 77 distinct category IDs from the doc's category set of 2025-11-09, e.g. `SR2025-categories-after-
   2025-11-09.md`) overrides the LLM category and may put a paper under several agendas.
4. **Final taxonomy differs from the one the LLM saw.** `data/taxonomy.yaml` in the repo is the final one (96 nodes,
   81 leaves, 8 sections; leaf IDs are the final agenda IDs). Only 45 of the 138 distinct top-category IDs the LLM
   produced exist in it (covering 426 of 3,138 items) [data]; the README warns about this. The LLM category column
   therefore cannot be used as an agenda label for the final taxonomy.
5. **Team editing.** "Work on the ~70 agendas was distributed among the team. We ended up making many local changes
   to the taxonomy, esp. splitting up and merging agendas." Attempts to have the AI propose a bottom-up taxonomy "weren't
   very good" [text].
6. **Agenda attributes** (summary, theory of change, orthodox problems, target case, broad approach, names, FTEs,
   critiques, funding) are human text; the LLM only parsed them from the edited document (`prompts/draft.yaml`).

## 5. Data usable as eval sets for triage and screen

Paths are relative to the repository root. "Final" means present in `papers.csv` (825 URLs, 764 distinct after
normalisation). URL join key: lowercase scheme/host, https, no `www.`, arXiv `abs/ID` without version or `.pdf`,
no fragment, trailing slash or tracking parameters (what `shallow_review/utils.py:normalize_url` does). The SR
repository's README gives its licence as "to be determined"; the data is public. The prompts quoted in Appendix A are
reproduced with the permission of their author and copyright holder, who maintains this repository (`README.md`,
"Licence").

| Path | Rows | Columns | Included / excluded | Reason recorded? | Use |
|---|---|---|---|---|---|
| `main-pipeline/data/2025-12-16-draft-post-review/papers.csv` | 826 (825 with URL; 820 with title) | `agenda_name, agenda_section, agenda_sub_section, agenda_full_name, link_url, link_text, title, authors, author_organizations, published_year, date` | all included; no excluded | no | positives with a human agenda label (sections: Black-box 222, White-box 202, Evals 183, Theory 65, Multi-agent 62, Labs 54, Make AI solve it 25, Safety by construction 13); title-only records for triage |
| `main-pipeline/data/2025-12-16-draft-post-review/agendas.csv` | 79 | `name, section, sub_section, full_name, one_sentence_summary, theory_of_change, see_also, orthodox_problems, target_case_text, broad_approach_text, some_names, estimated_ftes, critiques, funded_by, outputs_count` | n/a | n/a | agenda definitions (81 in the parsed JSON, which also holds China/Others labs) |
| `main-pipeline/data/2025-12-16-draft-post-review/source-parsed.json` (also `.yaml`) | 96 items (81 agendas, 15 sections), 826 outputs | agenda attributes plus per-output `link_url, link_text, original_md, title, authors, author_organizations, date, published_year, venue` | all included | no | same as `papers.csv`, with venue |
| `main-pipeline/data/data-dump-classify.csv` | 3,384 | `url, status, source, source_url, collect_relevancy, added_at, processed_at, kind, data (JSON), error, ai_safety_relevance, shallow_review_inclusion, url_hash, url_hash_short`; JSON holds title, authors, organisations, date, venue, `kind`, `contribution_type`, 1-2 sentence summary, key result, both scores, categories (old taxonomy), `category_comment` (the model's rationale), `confidence` | no decision column; join to `papers.csv`. 3,138 done rows (3,040 distinct URLs): 812 rows (751 URLs) are final, the rest not | model rationale only; no human reason | candidate pool with LLM scores; records of title plus short summary (no abstract, no full text); proxy labels, see below |
| `main-pipeline/data/data.db` (SQLite, same classify table plus `classify_feedback`, `collect`, `scrape`) | classify 3,384; feedback 1,561; collect 189; scrape 3,930 | feedback: `url, feedback_source, feedback_timestamp, action, human_category, paper_id, link_text, notes` | feedback: include 611, exclude 158 (154 URLs), reclassify 640, note 152 | `exclude`: auto-generated sentence, not a human reason; `include`: "Imported from ..."; `reclassify`: a category ID | the only explicit negative labels; weak (section 5.2) |
| `main-pipeline/data/2025-11-XX-internal-processing/SR2025-links-from-WIP-doc.csv` | 663 (611 distinct URLs) | `URL, CATEGORY_ID, PAPER_ID, LINK_TEXT` | the editors' keep list of 2025-11-09; 480 of the 611 are in the final | no | an intermediate human decision point; shows what changed between 11-09 and 12-16 |
| `main-pipeline/data/scraped/<sha256(url)>.html.zst` | 3,739 | stripped by `preprocess_html` before the LLM saw it | n/a | n/a | full text for screen items (pages as of Oct-Dec 2025) |
| `scraping-scripts/arxiv-scraper/results/2-arxiv_annotated_results.csv` | 368 | `title, authors, published, updated, summary (abstract), primary_category, categories, arxiv_id, pdf_url, doi, tags, relevance, reasoning` | LLM labels: highly 159, moderately 183, not 26; in final: 57, 8, 0 | model `reasoning` (gpt-4.1) | the only set with real abstracts and a final-membership proxy; 2025 arXiv papers matching "ai safety"/"ai alignment" |
| `.../results/6-arxiv_curated_with_flags.csv` | 150 | the above plus `is_new, duplicate_reasoning, matched_link, is_relevant, curation_reasoning, agenda` | `is_relevant` y 133 (49 in final), n 17 (0 in final) | model `curation_reasoning` (gpt-5-mini) | papers not already in the draft at the time, with abstract and a quality-bar verdict |
| `.../AF-and-LW-scraper/results/1-lw_raw_posts.csv` | 5,304 | `id, title, url, pageUrl, author, createdAt, score, commentCount, wordCount, tags, excerpt` (the last four columns empty) | 129 in final (2.4 %) | no | title-only triage set: realistic negatives (all LW posts of 2025, not only AI) with 129 positives |
| `.../results/2-lw_filtered_posts.csv`, `4-lw_curated.csv` | 637; 300 | same | 91 of the 129 final posts lie in both | no | the title-keyword and top-karma subsets |
| `.../results/sheets/all-alignment-forum-posts-2025-filtered.csv` | 438 | `title, link, karma, date, keep` | `keep` y 98 / blank 340; 27 and 48 are in final | no | provenance unknown, weakly aligned with the final decision; not a gold set |
| `main-pipeline/data/sources-links/*.txt`, `sources-various_data/*` | 10-1,449 lines each | URLs; names; `SR2024-some-names-ranked.csv`: `NAME, AGENDA, PROMINENCE` (259) | none | no | candidate lists; the researcher-name list for author sweeps |
| `main-pipeline/data/taxonomy.yaml` | 96 nodes, 81 leaves | `id, name, description, children` | n/a | n/a | the final agenda tree (SR 2025) |
| `main-pipeline/data/sources-various_data/SR2024.txt` | 1,326 lines | the 2024 review text | n/a | n/a | prior-year examples |

### 5.1 What the pool looks like against the final decision [data]

Restricting to URLs that reached the pool through a source independent of the final document (the nine non-draft
sources of 1.2; distinct normalised URLs, status `done`): 2,645 URLs, of which 507 are in the final by URL (19.2 %);
60 more match a final title under a different URL, so about 567 (21 %) are true positives. Against URL membership:

| LLM cut on (ASR, SRI) | URLs selected | In final | Precision | Recall of the 507 |
|---|---|---|---|---|
| ASR at or above 0.1, SRI at or above 0.4 (the scaffold cut) | 870 | 496 | 0.57 | 0.98 |
| 0.5, 0.5 | 761 | 471 | 0.62 | 0.93 |
| 0.7, 0.5 | 634 | 398 | 0.63 | 0.79 |
| 0.7, 0.7 | 488 | 329 | 0.67 | 0.65 |

The scaffold cut has high recall and modest precision on this measure, which matches its purpose (feed the editors).
Share of URLs in the final by the LLM's `kind`: `paper_preprint` 37 % (348 of 951), `lesswrong` 28 % (84 of 304),
`paper_published` 18 % (9 of 49), `dataset_benchmark` 18 % (3 of 17), `agenda_manifesto` 16 % (5 of 32), `code_tool` 14 %
(2 of 14), `blog_post` 9 % (45 of 498), `news_announcement` 2 % (4 of 252); `social_media` (0 of 79), `commercial` (0 of
46), `podcast`, `video`, `personal_page`, `course_educational` 0. By `contribution_type`: `empirical_results` 40 %
(313 of 788), `theoretical_framework` 36 %, `benchmark_dataset` 29 %, `position_agenda` 21 %, `tool_software` 19 %,
`critique` 12 %, `survey_review` 9 %, `demo_case_study` 7 %, `other` 1 % (9 of 985).

### 5.2 Cautions for using it as ground truth

1. **A negative is "not in the final", not "rejected".** No human reason exists for any exclusion. Three things make
   it noisier: the 154 explicit `exclude` URLs come from omission at 2025-11-09 and 46 of them (30 %) are in the
   final anyway; some omissions are plausibly editorial (an agenda already covered, publication outside the window,
   a duplicate under another URL) rather than out of scope; and 125 independent-source URLs have ASR and SRI both at
   or above 0.7 and are absent from the final by URL and title, among them "Alignment faking in large language
   models" (the lab-site URL), "Inverse Scaling in Test-Time Compute", "Avoiding Obfuscation with Prover-Estimator
   Debate" and "The Singapore Consensus on Global AI Safety Research Priorities". Why they are absent is not recorded.
2. **Positives include things the LLM stage scored low.** Of 751 final URLs with a classify record, 78 (10.4 %)
   have SRI below 0.4: 29 are scrape failures scored 0 (`error_detected`, `blocked`; for instance a final
   Infra-Bayesian paper post and a "429 Too Many Requests" page), 49 are readable pages the LLM judged below the cut,
   among them "Infra-Bayesianism" (ASR 0.82, SRI 0.15), "The Platonic Representation Hypothesis" (0.35, 0.30), "Agent
   foundations: not really math, not really science" (0.92, 0.45), "GDPval" (0.25, 0.30), "Remote Labor Index"
   (0.30, 0.35), "Characterising Simulation-Based Program Equilibria" (0.52, 0.48). They reached the final through
   human additions or forced inclusion, mostly via the draft-derived sources. These are the closest thing in the
   data to the "extension" items of `shallow-review.md` section (c) and its flip list in (f).
3. **Circular sources.** Do not use `draft-2025-11-19`, `draft_parser`, `feedback-*` rows as independent candidates.
4. **Scores of unreadable pages are meaningless**: 257 `done` rows have `kind = error_detected` and 70 `blocked`
   (scores typically 0). Drop them from threshold analyses.
5. **No abstract or full text** in the dump: the `data.summary` is the model's 1-2 sentences. Abstracts exist only
   for the arXiv sweep; full text only via the scraped HTML, which needs the same URL hash.
6. **The window** is about 2024-11-30 to 2025-11-30 for the final, but the pool has 323 items with year 2024, 430
   undated and a few older; a window rule has to be applied before comparing.

## 6. Differences

### 6.1 Against `docs/scope/shallow-review.md` (written from the website)

Confirmed by the repository: the two-score pre-filter, intent over method, the in/out lines of the inclusion bar
(technical critiques high, authoritative lab frameworks high, system cards below 0.4, general capabilities low,
governance position papers low), "LLM pre-filter then a full manual pass", the 3,384-row pool and its source counts
(1,561 Zvi; 377 + 186 researcher lists; 293 LW; 279 Lenz).

Differences and additions:

1. **The LLM stage is more permissive than the published scope.** The classify and collect prompts list as relevant
   "governance-adjacent technical work (evals, standards, safety cases)", robustness and adversarial examples, value and
   preference learning, RLHF and reward modelling, "AI forecasting and timelines (when relevant to safety)" and multi-
   agent safety; the Methods text excludes "technical policy", "standards, legislation", "general capabilities
   evals". The editors' pass, not the prompt, applies the published exclusions. The note's "SR also excludes general
   capabilities evals" holds for the text, but the pre-filter scored evals and forecasting generously (`evals_capability`
   was the second most frequent top category, 319 items; forecasting with methodology gets SRI 0.50-0.75).
2. **Candidate sources differ from the note's list.** The note lists an arXiv keyword sweep, ~120 researchers, all AF
   posts, LW posts under the "AI" tag, four newsletters, Twitter and conference pages. In the code: the arXiv keyword
   sweep is a separate late gap-check (368 papers, Jan-Oct 2025) with its own GPT filters, and only 102 of its
   papers are in the classify table; the researcher lists are 110 SR2024 names plus two small name lists; AF is one
   438-row sheet; LW is a title-only keyword filter over **all** LW posts (no tag), top 300 by karma; of the four
   newsletters only Zvi (181 posts, the largest source) and Lenz (a paper list) have inputs; there is no Twitter
   scraper, no conference-page list and no Gasteiger or Paleka input. "steerability" does not occur in the sweep code.
3. **"Manual additions (~480)" in section (e) of the note is mislabelled.** The three draft-derived sources sum to
   477 rows (187 + 133 + 157): links read back out of the review draft, including critiques, funders and lab pages.
   The manual dumps and one team member's arXiv list are 211 rows (57 + 93 + 61).
4. **Numbers.** `papers.csv` has 826 rows (README: 827); the Methods text says "2000+ links ... about 700 made it
   in", versus 3,384 classify rows (3,138 classified) and 764 distinct final URLs. Export window in code is
   2024-11-01 onward, not 2024-11-30.
5. **Hard negatives.** The note suggests `data-dump-classify.csv` as "a source of hard negatives". It has no
   rejection labels; see 5.2. It works as a recall set (751 of 764 final URLs are classified, 5 failed, 8 absent) and as
   a set of unlabelled candidates.
6. **Thresholds and models are now known**: SRI at or above 0.4 and ASR at or above 0.1 for the scaffold; Claude
   Sonnet 4.5 with a 2,048-token thinking budget (code default; per-row model not recorded).
7. **Support for the extension items is thinner in the prompts than in the taxonomy.** The classify prompt names
   agent foundations and "theoretical alignment work", "foundational theory" (SRI 0.5-0.7), "Gradual Disempowerment"
   and "Multi-Agent Risk" as borderline-technical topics; it does not mention decision theory, anthropics, acausal
   trade, ecology, biology or behavioural economics. Items of that kind that reached the final scored low (section
   5.2, point 2), so SR's own pre-filter would not have been a good guide to the extension [inferred from those
   examples; no systematic test].
8. **Agenda labels.** The note treats `taxonomy.yaml` as SR's classification taxonomy. It is the final one; the LLM
   assigned categories from an earlier 103-leaf tree (only 45 of 138 produced IDs survive), and the final agenda
   assignment is human (section 4).

### 6.2 Against `evals/rubrics/triage.md` and `screen.md` (v1; `*-v2.md` already widen to the SR scope and were not compared line by line)

| Aspect | SR 2025 repository | Rubrics v1 |
|---|---|---|
| Anchor | topic relevance to preventing large unintended effects of advanced AI, "intent over method", agenda-level | a safety *technique* that a methods catalogue could have an entry for |
| What is scored | two scores: ASR (topic) and SRI (original technical contribution), plus `confidence` | one 3-way scope verdict (`relevant`/`borderline`/`not_relevant` or `irrelevant`) plus confidence; "Source quality does not matter for scope" |
| Quality | inclusion explicitly rewards originality and technical depth and penalises news, commentary, summaries, system cards | scope only; vendor blogs, forum posts and unreviewed preprints count if they carry technique evidence |
| System cards, release notes, safeguards pages, marketing | system cards SRI below 0.4; `commercial`, `news_announcement` kinds exist and are almost never in the final (0 of 46 commercial, 4 of 252 news) | relevant (deployment signal) |
| Governance / policy | out unless technical depth; "evals, standards, safety cases" are named as in-scope technical work in the prompt | in when it names or commits to a concrete safeguard |
| Theory, agent foundations | explicitly in (SRI 0.5-0.7, moderate confidence); several final theory items were nevertheless scored below the 0.4 cut (5.2) | theory counts only "aimed at aligning such systems" (v1) |
| Critiques | technical critiques scored high (above 0.7) | "Judgement" is one of five in-scope kinds |
| Social media | major author announcements only | silent |
| Date | collect prompt: 2025 plus Nov-Dec 2024, inclusive when unclear; export cut 2024-11-01 | none |
| Gate granularity | collect: link text plus a one-sentence context from the aggregator page, threshold 0.3, no abstract; classify: full stripped page (up to 100,000 tokens) | triage: title, authors, abstract/excerpt; screen: first 8,000 characters |
| Tie-breaks | "Be inclusive rather than exclusive" at collect; "Be conservative with inclusion scores" at classify; scores continuous | explicit cost-asymmetry tie-breaks toward keeping |
| Outputs | `kind` (17), `contribution_type` (9), summary, key result, 1-3 categories, rationale paragraph, confidence | `verdict`, `buckets` (screen), confidence, rationale, quote |
| Evidence requirement | reasoning is requested ("quote key passages in your thinking"), the quote is not output | a verbatim `quote` is required |

SR's `kind` set maps loosely onto the screen buckets (`paper_*`, `lesswrong`, `blog_post` to `paper`/`critique_commentary`;
`news_announcement` to `release_news`; `agenda_manifesto` to `lab_safety_doc` or `critique_commentary`); SR has no
bucket for incidents or deployment signals. The decisive conflict for a combined rubric is the quality axis: SR's
SRI penalises exactly the document types (system cards, release news, vendor blogs) that the Stack rubric treats as
primary evidence, so a single label cannot serve both without a scope flag.

## Appendix A. Prompt passages, verbatim

Reproduced with the permission of their author and copyright holder, the maintainer of this repository (`README.md`,
"Licence"). Extracted programmatically from the clone so that wording and line numbers are exact. Total prompt volume of the
relevance and inclusion stages exceeds 600 lines, so only the decisive parts are quoted; `prompts/draft.yaml` (383
lines, attribute extraction from the edited document) and the duplicate-check prompt are not inclusion criteria and
are omitted.


### A.1 Classify prompt

#### A.1a System prompt

`main-pipeline/prompts/classify.yaml`, lines 5-7

````yaml
    You are an expert AI safety and AI alignment researcher tasked with analyzing and classifying research content for potential inclusion in "Shallow Review 2025" - an annual review of technical AI safety research.
    
    Core definition: We target "work that intends to prevent very competent cognitive systems from having large unintended effects on the world."
````

#### A.1b Task steps

`main-pipeline/prompts/classify.yaml`, lines 30-36

````yaml
    Analyze this content and provide complete classification metadata following these steps:
    
    1. **Understand the content**: What is this work actually doing? What are its main contributions?
    2. **Assess technical depth**: Does this present original technical research, technical agendas, or is it commentary/news about research?
    3. **Evaluate safety relevance**: How directly does this relate to preventing unintended effects from advanced AI systems?
    4. **Match to taxonomy**: Which leaf categories best fit the actual technical work being done?
    5. **Apply Shallow Review standards**: Would this belong in a premier technical review of AI safety research?
````

#### A.1c Scope and common pitfalls

`main-pipeline/prompts/classify.yaml`, lines 38-66

````yaml
    # AI Safety and Alignment Scope
    
    **Relevant work includes:**
    - Technical AI alignment and safety research
    - Interpretability and understanding of AI systems  
    - AI control, monitoring, and evaluation methods
    - Multi-agent AI safety and coordination
    - Formal methods, verification, and provable safety
    - Agent foundations and theoretical alignment work
    - Governance-adjacent technical work (evals, standards, safety cases)
    - Borderline technical topics offering novel causal perspectives on AI X-risk
    
    **Intent over method:** If work aims to understand or prevent unintended effects from advanced AI systems, it's relevant, regardless of specific technical approach or framing.
    
    # Common Classification Pitfalls
    
    **Forecasting vs evals:** Forecasting is NOT evaluation (evals test current models), BUT technical capability forecasting that develops statistical models, analyzes eval/benchmark trends, or otherwise contributes technical methodology gets MODERATE-HIGH inclusion (0.50-0.75) as it significantly informs technical research. Pure timelines speculation gets low inclusion.
    
    **Capabilities vs safety:** General capabilities work (agent frameworks, optimization tools, coding benchmarks, formal verification of general code) without explicit safety connection gets LOW safety relevance (<0.4) and LOW inclusion (<0.4). Even if tangentially useful for safety research, judge primarily by whether safety is the focus. Long-horizon reasoning/planning is NOT automatically safety-relevant unless connected to autonomous agent deployment, dangerous capabilities, or safety properties.
    
    **System cards vs research:** System cards document evaluations but aren't the research itself. Score as news/documentation (SR <0.4) unless exceptionally detailed with novel methodology.
    
    **Authoritative frameworks:** Preparedness frameworks, RSPs, and technical agendas from major labs/orgs (OpenAI, Anthropic, OpenPhil, Redwood, MIRI, etc.) get high inclusion (0.70-0.85) as they authoritatively inform research direction, even if not presenting original research.
    
    **Commentary vs research:** Overview posts and news summaries get low inclusion (<0.4). Technical critiques from a technical standpoint (feasibility, promise) get HIGH inclusion (>0.7). Position papers vary: those with technical depth or proposing new technical agendas get moderate-high inclusion (0.5-0.75); those discussing approaches from governance/policy perspective without technical depth get low inclusion (0.3-0.5).
    
    **Foundational theory:** Abstract theoretical work on interpretability limits, computational properties, or formal foundations gets moderate-high inclusion (0.5-0.7) even if not directly applicable yet. Use moderate confidence (0.5-0.7) for such work.
    
    **Access errors:** If content unavailable (errors, paywalls, format issues), set `kind="error_detected"` and `confidence=0.0`.
````

#### A.1d Scoring guidelines (ASR and SRI)

`main-pipeline/prompts/classify.yaml`, lines 68-112

````yaml
    # Key Scoring Guidelines
    
    ## AI Safety Relevance (0.0-1.0)
    
    How relevant is this content to AI safety/alignment as a topic area?
    
    - **0.9-1.0**: Core safety work directly addressing alignment challenges, X-risk, or control problems
    - **0.7-0.9**: Clearly safety-focused work with direct implications for AI safety
    - **0.5-0.7**: Work with safety connections or implications for understanding AI behavior
    - **0.3-0.5**: General AI research with some safety relevance or tangential connections
    - **0.1-0.3**: Mainstream AI/ML with minimal safety connection (general capabilities, optimization tools, coding benchmarks without safety framing)
    - **0.0-0.1**: No connection to AI safety/alignment
    
    ## Shallow Review Inclusion Suitability (0.0-1.0)
    
    How suitable is this for inclusion in Shallow Review 2025? Focus on technical depth and original contribution - the "Shallow Review spirit":
    
    **Core inclusion criteria:**
    - ✅ Original technical research (empirical or theoretical, including foundational theory)
    - ✅ Technical research agendas, roadmaps, or frameworks
    - ✅ Novel technical tools, benchmarks, or datasets for safety research
    - ✅ Technical critiques with substantive analysis
    - ✅ Foundational theory work on interpretability limits, computational properties, etc.
    - ❌ News/commentary about research (unless unclear if we have the original)
    - ❌ General capabilities work: agent frameworks, optimization tools, coding benchmarks, formal verification of general code without safety focus
    - ❌ Position papers discussing approaches without technical depth (governance-oriented)
    - ❌ Opinion pieces or think pieces without technical contribution
    - ❌ Summaries or reviews without novel insights
    - ❌ Mainstream AI work tangentially mentioning safety
    
    **Scale dimensions** (judge by these qualities, not content type alone):
    - **Originality**: Novel research, methods, tools vs. describing others' work
    - **Technical depth**: Formal frameworks, empirical results, implementation vs. high-level discussion
    - **Contribution type**: Direct technical contribution vs. meta-level commentary or synthesis
    - **Actionability**: Informs concrete technical work vs. general perspectives
    
    **Rough scale anchors:**
    - **0.9-1.0**: Major original technical contribution (novel empirical results, comprehensive frameworks, significant theoretical advances)
    - **0.7-0.9**: Solid technical work with clear contribution (good empirical work, useful tools, substantive technical critiques)
    - **0.5-0.7**: Moderate technical value (foundational theory, preliminary results, technical position papers, tool releases)
    - **0.3-0.5**: Limited direct technical contribution (high-level discussion of approaches, early-stage ideas, governance-focused analysis)
    - **0.1-0.3**: Minimal technical content (news coverage, commentary, summaries without novel insight)
    - **0.0-0.1**: No technical contribution relevant to research
    
    **Key principle**: Judge by technical contribution quality, not content type. Authoritative news/frameworks or well-argued position papers can score higher than their type suggests; papers with weak contributions score lower. Content can be highly relevant to AI safety but unsuitable for Shallow Review if it lacks technical depth.
````

#### A.1e Reminders at the end of the prompt

`main-pipeline/prompts/classify.yaml`, lines 330-345

````yaml
    **Remember:**
    - Use only leaf category IDs from the provided taxonomy
    - Keep summary to 1-2 sentences max, key_result to 1 sentence max - be concise
    - General capabilities work without safety focus gets LOW scores (<0.4 for both ais and sr)
    - Autonomy evals require BOTH: autonomous agents AND safety relevance (not just long-horizon or coding)
    - Technical critiques from a technical standpoint get HIGH inclusion (>0.7) if substantive
    - Foundational theory work (interpretability limits, computational properties) gets MODERATE-HIGH inclusion (0.5-0.7) with moderate confidence
    - Position papers: distinguish technical depth vs governance/policy discussion - latter gets LOW inclusion (0.3-0.5)
    - Authoritative frameworks from major orgs get HIGH inclusion (0.70-0.85) even if not original research
    - System cards are documentation (SR <0.4), NOT the evaluation research itself
    - Technical capability forecasting with statistical models/methodology gets MODERATE-HIGH inclusion (0.50-0.75)
    - Be conservative with inclusion scores - original technical research should score higher than commentary
    - High safety relevance + low technical contribution = low inclusion score
    - Quote key passages in your thinking to ground your analysis
    - For access errors: use `kind="error_detected"` and `confidence=0.0`
    - Your final output should consist only of the JSON classification
````

### A.2 Collect prompt

#### A.2a Scope

`main-pipeline/prompts/collect.yaml`, lines 7-38

````yaml
    # AI Safety and AI Alignment Scope
    
    **Core Definition:** We target "work that intends to prevent very competent cognitive systems from having large unintended effects on the world."
    
    **Relevant topics include:**
    - Technical AI alignment: ensuring AI systems behave according to human values and intentions
    - AI safety: making AI systems robust, reliable, and safe
    - Existential risk from advanced AI (x-risk, AGI safety)
    - Interpretability and understanding of AI systems
    - AI control, monitoring, and evaluation methods
    - Multi-agent AI safety and coordination
    - Formal methods, verification, and provable safety for AI
    - Agent foundations and theoretical alignment work
    - Robustness, adversarial examples, and security
    - Value learning and preference learning
    - Reward modeling and RLHF (Reinforcement Learning from Human Feedback)
    - Scalable oversight, debate, and recursive reward modeling
    - Governance-adjacent technical work (evals, standards, safety cases)
    - Machine learning safety techniques (e.g., safe exploration, distributional shift)
    - Borderline technical topics offering novel causal perspectives on AI X-risk (e.g., Gradual Disempowerment, Multi-Agent Risk scenarios)
    - AI forecasting and timelines (when relevant to safety)
    
    **Intent over method:** If work aims to understand or prevent unintended effects from advanced AI systems, it's relevant, regardless of specific technical approach or framing.
    
    **NOT relevant (examples):**
    - General machine learning or AI capabilities research (unless it explicitly discusses safety implications)
    - AI applications in specific domains (healthcare, finance, etc.) unless safety-focused
    - Fairness and bias in current AI systems (unless framed as alignment/safety research)
    - General tech news or company announcements
    - Clickbait or promotional content
    - Navigation links, admin pages, login pages
    - Social media commentary, opinions, discussions (see special criteria below)
````

#### A.2b Link scoring and guidelines

`main-pipeline/prompts/collect.yaml`, lines 56-82

````yaml
    2. **Extracted links:** For each link that **might** be relevant to AI safety/alignment:
       - `url`: The full URL (resolve relative URLs to absolute)
       - `link_text`: The exact text of the link (verbatim, as it appears in <a> tag)
       - `context`: One sentence describing what the link is about and why it might be relevant (summarize surrounding text, headings, descriptions)
       - `ai_safety_relevancy`: A score from 0.0 to 1.0 indicating likelihood of relevance:
         - `0.9-1.0`: Almost certainly relevant (explicit AI safety/alignment paper or post)
         - `0.7-0.9`: Likely relevant (discusses alignment concepts, safety techniques)
         - `0.5-0.7`: Possibly relevant (AI research with potential safety implications)
         - `0.3-0.5`: Uncertain (might be relevant, needs closer inspection)
         - `0.1-0.3`: Probably not relevant (tangentially related at best)
         - `0.0-0.1`: Almost certainly not relevant
    
    # Guidelines
    
    - **DATE FILTER (IMPORTANT)**: Only collect material from 2025 and from November-December 2024. Skip older content. If publication date is unclear from link text/context, be inclusive and include it.
    - **Be inclusive rather than exclusive**: If unsure whether something is relevant (score ≥ 0.3), include it. False positives are okay; we'll filter later.
    - **Ignore obvious non-content**: Skip navigation menus, footer links, social media buttons, login/admin links, advertisements, "Back to top" links, etc.
    - **Focus on substantial content**: Prioritize links to papers, blog posts, articles, talks, courses, research agendas
    - **Social media special criteria**: For Twitter/X, Reddit, or similar platforms, ONLY include links that are:
      - Major announcements (new research agendas, breakthrough results, paper releases)
      - From researchers/organizations announcing their own work
      - NOT commentary, opinions, threads analyzing others' work, or discussions
      - Examples to INCLUDE: "Announcing our new interpretability agenda...", "Released: paper on X-risk..."
      - Examples to EXCLUDE: "Here's my take on...", "Thread: why approach X won't work...", "Great paper by @someone..."
    - **Resolve relative URLs**: Convert all relative URLs to absolute URLs using the page's base URL
    - **Be concise but informative**: Context should be one clear sentence explaining what the link is about
    - **Use link text and surrounding context**: Look at headings, descriptions, abstracts, or surrounding paragraphs to understand what the link points to
````

#### A.2c Closing instruction

`main-pipeline/prompts/collect.yaml`, line 171

````yaml
    Extract the page metadata (title, kind, comments) and all links with ai_safety_relevancy ≥ 0.3.
````

### A.3 arXiv sweep, step 2 (relevance filter)

#### A.3 SYSTEM_PROMPT

`scraping-scripts/arxiv-scraper/2-filter_results.py`, lines 30-68

````python
SYSTEM_PROMPT = """You are an expert in technical AI alignment research helping to conduct a 2025 shallow review of the field.

Your task is to analyze arXiv papers and determine their relevance to technical AI alignment work. We are specifically interested in papers that:

**Core Technical AI Alignment Areas:**
- AI safety and alignment methodologies (RLHF, RLAIF, constitutional AI, etc.)
- Mechanistic interpretability and transparency
- Scalable oversight techniques
- Adversarial robustness and jailbreaking
- Model evaluation and benchmarking for safety
- Reward hacking and specification gaming
- Deceptive alignment and misalignment
- Value alignment and preference learning
- AI control and monitoring
- Red teaming and adversarial testing

**Novel Contributions We're Looking For:**
- New techniques, frameworks, or methodologies
- Empirical findings that advance understanding
- Novel theoretical insights
- Practical tools and benchmarks
- Case studies revealing important failure modes
- Interdisciplinary approaches bringing new perspectives

**What to Deprioritize:**
- Pure capabilities research without safety implications
- Generic ML papers not specifically addressing alignment
- Applications of AI in specific domains (unless they reveal alignment insights)
- Papers that only tangentially mention safety/alignment
- Purely philosophical discussions without technical content

For each paper, provide:
1. **Tags**: 2-5 specific tags categorizing the paper's contribution areas
2. **Relevance**: Rate as "not relevant", "moderately relevant", or "highly relevant"
   - "highly relevant": Core technical alignment work with novel contributions
   - "moderately relevant": Related to alignment but peripheral or applied
   - "not relevant": Not about AI alignment or safety

Be selective - only rate papers as "highly relevant" if they make meaningful technical contributions to AI alignment."""
````

### A.4 arXiv sweep, step 5 (curation)

#### A.4 SYSTEM_PROMPT

`scraping-scripts/arxiv-scraper/5-curate-unique-arxiv-links.py`, lines 35-74

````python
SYSTEM_PROMPT = """You are an expert curator for a 2025 shallow review of technical AI alignment research.

Your task is to evaluate whether newly discovered arXiv papers should be included in the review document. The review aims to cover the most important and relevant technical AI alignment work from the period.

**Evaluation Criteria:**

1. **Relevance & Quality Bar** - The paper must be:
   - Directly relevant to technical AI alignment research
   - High-quality with novel contributions or important empirical findings
   - Interesting enough to merit inclusion in a curated review
   - Not redundant with content already covered in the post

2. **Contribution Type** - Determine if the paper:
   - **Contributes to existing agenda**: Advances topics/themes already discussed in the post
   - **Contributes to new agenda**: Introduces important new research directions (HIGH BAR - must be significant)
   - **Neither**: Not relevant enough or quality enough to include

**Agenda Categories** (common themes in AI alignment):
- Interpretability / Mechanistic interpretability
- Scalable oversight
- Evaluations / Benchmarking
- Adversarial robustness / Red teaming / Jailbreaking
- RLHF / RLAIF / Alignment techniques
- Deceptive alignment / Misalignment
- Safety / AI safety
- Control theory / AI control
- Multimodal safety
- Model evaluation
- Constitutional AI / Value alignment
- Reward hacking / Specification gaming
- etc.

For each paper, provide:
1. **is_relevant**: "y" if the paper should be included in the review, "n" if not
2. **reasoning**: Brief explanation of your decision (2-3 sentences)
3. **agenda**: Comma-separated keywords describing the paper's alignment agendas (empty if not relevant)

Be selective - only mark papers as relevant if they make meaningful contributions worthy of inclusion in a curated review.

Do not add low-quality or uninteresting papers that would not add value to the review."""
````

### A.5 Alignment Forum post filter (notebook)

`scraping-scripts/AF-and-LW-scraper/notebooks/AF-AI-post-filtering.ipynb`, code cell 3, variable `prompt` (model `gpt-5-mini`, reasoning effort `minimal`, batches of 10 posts with the first 500 words of each; the user message adds "Remember to keep approximately 1 in 10 posts.")

````text
I'm writing a literature review of the field of AI alignment for 2025. My goal is to find the most interesting posts from the past year that represent advancements in the field.

You should go through the list of posts and identify posts that seem like an advancement in the field of AI alignment.

Here are categories of posts you should keep:
- Posts that link to a paper or introduce an advancement in the field of AI alignment
- Posts that have high karma
- Posts that are high-quality critiques of a specific research agenda
- High-quality literature reviews
- High quality position posts

For each batch of 10 posts, you should typically keep 1-3 of the best posts in the group.

If there are multiple exceptionally high-quality posts, you may keep up to 5 posts.

For each post, you should output a JSON object with the following fields:
- keep: true if the post should be kept, false otherwise
- reason: a short reason for keeping or not keeping the post
- category: the category of the post (e.g. "critique", "review", "position", "advancement")
- field: the field of AI alignment that the post is about (e.g. "interpretability", "RL safety", "agent foundations" etc.). Note that you may discover new fields of AI alignment that are not already in the list.
````

### A.6 URL typing when adding items

#### A.6 Decision guidelines

`main-pipeline/prompts/add_items.yaml`, lines 35-46

````yaml
    # Decision Guidelines
    
    - **AI safety context matters**: We only care about AI safety/alignment links
    - General ML conference page → likely single_content (too broad, few safety links)
    - AI safety workshop page → likely collection_source (focused, many safety links)
    - **When uncertain, prefer "single_content"** (safer - avoids extracting irrelevant links)
    - If page lists multiple distinct AI safety works → collection_source
    - If page is about ONE work (even with references) → single_content
    - Organization "research" page with project list → collection_source
    - Organization "about" page → single_content
    - Author publication list → collection_source (if safety-focused)
    - Individual paper with bibliography → single_content
````
