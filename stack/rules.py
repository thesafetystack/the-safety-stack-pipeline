"""Source rules (track S, S5; each stage may have its own rules, clearly delineated in code, over
keywords, types and channels): which documents a stage treats by **where they come from** rather than by asking an
LLM. Every rule lives in :data:`RULES`, keyed by the stage that reads it; nothing else in the
pipeline names a source list.

**Shapes.** A :class:`Match` is the AND of its non-empty criteria, each an OR over its list:

* ``channels`` — any member channel (``identity.channels``) is listed;
* ``url_prefixes`` — some ``url`` key of the document (``identity.keys``) starts with one. Keys
  are in :func:`stack.urls.url_key` form (``https``, lower-case host without ``www.``, no trailing
  slash, GitHub owner/repo lower case), so a prefix is written in that form plus ``/``; a member's
  ``canonical_url`` is its fetch form (as listed, ``www.`` and all) and is not compared (url-normalisation.md §2);
* ``authors`` — some ``metadata.authors[].name``, trimmed and lowercased, equals one (whole
  names: ``Zvi`` does not match ``Marjan Ghazvininejad``);
* ``title_keywords`` — a case-insensitive substring of ``metadata.title``;
* ``doc_types`` — ``metadata.doc_type`` is listed;
* ``keys`` — the document carries one of these ``(kind, value)`` identity keys (``identity.keys``,
  values compared as stored: in the kind's normal form, e.g. ``("flag", "accept")`` from
  ``collect.manual``).

A :class:`SourceRule` matches a row when some ``any_of`` match does and its ``exclude`` match (if
any) does not. :meth:`SourceRule.sql` compiles it to one duckdb predicate over a ``documents`` row
(the triage gate view's columns), every literal quoted by :func:`sql_literal`; a NULL field never
matches a criterion, and a NULL never turns an ``exclude`` into a match.

**Who reads which rules.**

* ``triage``: a plan's gate names a rule as a clause ``{rule: <name>}`` (``stack/plan.py``
  ``GateClause``); it behaves as a ``sql:`` clause whose SQL is :meth:`SourceRule.sql`
  (``stack/gate.py``). The compiled SQL is part of the gate's digest, so editing a list here
  re-scopes triage (``stack run triage --only select``, no LLM calls).
* ``links`` (``collect.links``, round 4 (L)): which documents are **linking documents** whose
  outbound links are harvested — the rules named in :data:`LINK_RULES_ON` (or the stage line's
  ``options.rules``), evaluated with the same compiled SQL over the ``documents`` head. The same
  section holds the link lists that are not document rules (self hosts, content regions, noise
  hosts, navigation paths, excluded link kinds): ``stack/collect/links.py`` reads them.
* ``collect`` (round 5 (RUN) B7a; collection-yield.md C6, C9, Tomáš 2026-10-13): the **safety
  stems** :data:`SAFETY_STEMS` (moved here from :mod:`stack.evals.freeze`, whose relevance proxy
  reads them, so the collectors' filters and the yield measurements use one list): the arXiv
  category family keeps a hit only with a stem (or an umbrella/SR phrase) in its title or abstract,
  and ``collect.blogs`` keeps a ``google_research`` post only with a stem in its title
  (:func:`safety_stem_regex`).
* ``collect``, keyword families (the backfill cuts of 2026-10-13, docs/design/collection-yield.md §10;
  D304: the proposed cuts accepted): :func:`keyword_cut` decides whether a hit of an OpenAlex or
  Semantic Scholar concept-group query is dropped, and by which cut. The vocabularies are here: the AI terms
  (:data:`AI_TERMS`, :func:`has_ai_term`), the DOI prefixes of self-publication and preprint servers
  (:data:`SELF_PUBLICATION_DOI_PREFIXES`, :data:`KEPT_PREPRINT_DOI_PREFIXES`) and the arXiv repository's provider
  ids (:data:`OPENALEX_ARXIV_SOURCE_IDS`, :data:`S2_ARXIV_VENUES`). Which cuts a channel applies, and to which
  groups, is channels.yaml (``keyword_cuts`` on the channel). The dated exception of D654: a hit
  published before ``keyword_cuts.publisher_cut_floor`` with a **pre-2020 safety stem** (:data:`PRE2020_SAFETY_STEMS`,
  :func:`publisher_cut_exempt`) bypasses the source cuts; ``ai_term`` still applies. The venue allowlist
  (the Chinese-coverage work): with ``keyword_cuts.keep_venues`` a hit with a DOI of :data:`VENUE_DOI_PREFIXES` (ACL Anthology, AAAI, IJCAI, NDSS, PLOS)
  is source class ``venue``, which no cut drops; :func:`venue_allowlist_digest` is in the coverage key.
* ``collect``, Chinese text (the Chinese-coverage work): :data:`ZH_SAFETY_STEMS` (part of :data:`SAFETY_STEMS`) and :data:`AI_TERMS_ZH`
  are matched as substrings (no word spaces, no ``\b``), and the English regexes are compiled ``re.ASCII`` so a term
  next to a Chinese character keeps its word boundary ("LLM安全"). Since D751 the AI-term vocabulary's digest
  (:func:`ai_term_digest`) is in the coverage key of every ``ai_term`` group, so the hits an older vocabulary dropped
  are asked again (D744 had left it out; the allowlist re-sweeps those groups anyway).
* every other stage: no rules yet.

**Adding a rule.** Append a :class:`SourceRule` to its stage's tuple in :data:`RULES` with a
``why`` that states its reason (and the decision behind it); names are unique across stages (:func:`rule` refuses a
duplicate). For a triage rule, list it in each plan's gate where it applies.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass


class RuleError(ValueError):
    """An unknown rule, a rule of another stage, or a malformed rule."""


def sql_literal(s: str) -> str:
    """A duckdb string literal for ``s``: single quotes doubled (duckdb's standard strings take
    backslashes literally, so nothing else needs escaping); a NUL byte is refused."""
    if "\x00" in s:
        raise RuleError(f"rule literal {s!r} contains a NUL byte")
    return "'" + s.replace("'", "''") + "'"


def _list(items: Iterable[str]) -> str:
    return ", ".join(sql_literal(s) for s in items)


@dataclass(frozen=True)
class Match:
    """The AND of its non-empty criteria, OR within each (module docstring)."""

    channels: tuple[str, ...] = ()
    url_prefixes: tuple[str, ...] = ()
    authors: tuple[str, ...] = ()
    title_keywords: tuple[str, ...] = ()
    doc_types: tuple[str, ...] = ()
    keys: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        crit = (self.channels, self.url_prefixes, self.authors, self.title_keywords, self.doc_types)
        if not any(crit) and not self.keys:
            raise RuleError("a Match needs at least one criterion (an empty one would match every row)")
        if any(not isinstance(k, tuple) or len(k) != 2 or not all(isinstance(x, str) and x.strip() for x in k)
               for k in self.keys):
            raise RuleError(f"Match keys are (kind, value) pairs of non-empty strings, got {self.keys!r}")
        for c in crit:
            if isinstance(c, str):
                raise RuleError(f"Match criteria are tuples of strings, got the string {c!r}")
            if any(not s.strip() for s in c):
                raise RuleError(f"Match criterion {c!r} has an empty entry")

    def sql(self) -> str:
        """The duckdb predicate (never NULL: each criterion is ``coalesce``d to false)."""
        parts: list[str] = []
        if self.channels:
            parts.append(f"list_has_any(identity.channels, [{_list(self.channels)}])")
        if self.url_prefixes:
            ors = " OR ".join(f"starts_with(k.value, {sql_literal(p)})" for p in self.url_prefixes)
            parts.append(f"EXISTS (SELECT 1 FROM unnest(identity.keys) AS _k(k) WHERE k.kind = 'url' AND ({ors}))")
        if self.authors:
            names = _list(a.strip().lower() for a in self.authors)
            parts.append("EXISTS (SELECT 1 FROM unnest(metadata.authors) AS _a(a) "
                         f"WHERE lower(trim(a.name)) IN ({names}))")
        if self.title_keywords:
            ors = " OR ".join(f"contains(lower(metadata.title), {sql_literal(k.lower())})"
                              for k in self.title_keywords)
            parts.append(f"({ors})")
        if self.doc_types:
            parts.append(f"metadata.doc_type IN ({_list(self.doc_types)})")
        if self.keys:
            ors = " OR ".join(f"(k.kind = {sql_literal(kind)} AND k.value = {sql_literal(value)})"
                              for kind, value in self.keys)
            parts.append(f"EXISTS (SELECT 1 FROM unnest(identity.keys) AS _k(k) WHERE {ors})")
        return " AND ".join(f"coalesce({p}, false)" for p in parts)


@dataclass(frozen=True)
class SourceRule:
    """A named rule of one ``stage``: some ``any_of`` matches and ``exclude`` does not."""

    name: str
    stage: str
    any_of: tuple[Match, ...]
    exclude: Match | None
    why: str

    def __post_init__(self) -> None:
        if not self.any_of:
            raise RuleError(f"rule {self.name!r}: `any_of` is empty")

    def sql(self) -> str:
        """One duckdb predicate over a ``documents`` row (module docstring)."""
        pos = " OR ".join(f"({m.sql()})" for m in self.any_of)
        if self.exclude is None:
            return f"({pos})"
        return f"({pos}) AND NOT ({self.exclude.sql()})"


# ---------------------------------------------------------------- triage
#: The frontier labs' collector channels: ``collect.blogs`` (posts) and ``collect.cards`` (system
#: and model cards, policies).
LAB_CHANNELS: tuple[str, ...] = (
    "anthropic_alignment_blog", "openai_blog", "openai_alignment_blog", "transformer_circuits",
    "google_research", "mistral_blog", "deepmind_blog",
    "anthropic_cards", "openai_cards", "deepmind_cards", "meta_cards", "xai_cards", "deepseek_cards",
    "qwen_cards",
)
#: The same labs' sites, so a post arriving through another channel (``manual``, a forum link) is
#: covered; in ``url`` key form (``www.`` dropped, GitHub owners lower case). Taken from the
#: collectors' configured URLs (``workflows/collect/blogs/main.py``
#: ``SITE_SPECS``, ``workflows/collect/cards/main.py`` ``WATCH_SPECS``; ``tests/test_source_rules.py``
#: holds every entry to one): the host of each surface or pinned URL (Google Research: the blog
#: path only, since the host also serves the publication pages the gate leaves to the question),
#: and for the GitHub surfaces the ``raw.githubusercontent.com/<owner>/`` root the cards collector
#: builds its URLs on.
LAB_URL_PREFIXES: tuple[str, ...] = (
    "https://alignment.anthropic.com/",         # anthropic_alignment_blog
    "https://anthropic.com/",                   # anthropic_cards (system-cards, rsp-updates)
    "https://transformer-circuits.pub/",        # transformer_circuits
    "https://openai.com/",                      # openai_blog, openai_cards
    "https://alignment.openai.com/",            # openai_alignment_blog
    "https://deploymentsafety.openai.com/",     # openai_cards
    "https://research.google/blog/",            # google_research (blog only: research.google/pubs/ is papers)
    "https://mistral.ai/",                      # mistral_blog
    "https://deepmind.google/",                 # deepmind_blog, deepmind_cards
    "https://ai.meta.com/",                     # meta_cards (frontier framework)
    "https://raw.githubusercontent.com/meta-llama/",   # meta_cards (llama-models, PurpleLlama)
    "https://x.ai/",                            # xai_cards
    "https://data.x.ai/",                       # xai_cards (pinned archive)
    "https://raw.githubusercontent.com/deepseek-ai/",  # deepseek_cards
    "https://raw.githubusercontent.com/qwenlm/",       # qwen_cards
)
#: The safety and research organisations' blog channels of ``collect.blogs`` (D195).
SAFETY_ORG_CHANNELS: tuple[str, ...] = (
    "miri_blog", "metr_blog", "transluce_blog", "goodfire_blog", "uk_aisi_blog", "redwood_blog",
    "apollo_blog", "arc_blog", "epoch_blog", "forethought_blog", "far_ai_blog", "timaeus_blog",
    "clr_blog", "softmax_blog", "meaning_alignment_blog", "cooperative_ai_blog",
)

#: The newsletter feeds of ``collect.feeds`` (``kind: feed`` channels; round 4 (L)).
NEWSLETTER_CHANNELS: tuple[str, ...] = ("zvi_substack", "import_ai", "paradigm3_news", "aisafetychina_substack")

TRIAGE_RULES: tuple[SourceRule, ...] = (
    SourceRule(
        "frontier_lab_posts", "triage",
        any_of=(Match(channels=LAB_CHANNELS), Match(url_prefixes=LAB_URL_PREFIXES)),
        exclude=None,
        why="2026-10-12 calibration: fetch and screen every post of the labs' blogs"),
    SourceRule(
        "safety_org_posts", "triage",
        any_of=(Match(channels=SAFETY_ORG_CHANNELS),),
        exclude=None,
        why="2026-10-12: fetch by source; fetching ~1000 blog docs is cheap, the bulk of "
            "irrelevant papers is what the question gate avoids"),
    SourceRule(
        "zvi_posts", "triage",
        any_of=(Match(channels=("lesswrong", "alignmentforum"), authors=("Zvi",)),),
        # His non-AI series seen in the dev store; an excluded post is not skipped, it falls
        # through to the plan's next clause (the LLM question).
        exclude=Match(title_keywords=("Childhood and Education",)),
        why="2026-10-12: fetch Zvi's posts by source unless clearly unrelated to AI; his non-AI "
            "series are excluded and go to the question"),
    # Track L (round 4): newsletter feeds (collect.feeds) and hand-added sources (collect.manual).
    SourceRule(
        "newsletter_posts", "triage",
        any_of=(Match(channels=NEWSLETTER_CHANNELS),),
        # zvi_substack carries all of Zvi's posts; his off-topic series as in zvi_posts go to the question.
        exclude=Match(title_keywords=("Childhood and Education",)),
        why="2026-10-12: the link sources are Zvi's posts, Import AI (jack-clark.net) and Paradigm3 "
            "news; the newsletters themselves enter the corpus as documents"),
    SourceRule(
        "manual_accept", "triage",
        any_of=(Match(keys=(("flag", "accept"),)),),
        exclude=None,
        why="2026-10-12: a manual source carries flags to collect its links and to be accepted in "
            "triage (overrides/manual_sources.yaml, flag `accept`)"),
    # Track L (round 4, L2): link targets collect.links selected (context-aware LLM selector).
    SourceRule(
        "selected_links", "triage",
        any_of=(Match(channels=("links_url", "links_forum", "links_paper")),),
        exclude=None,
        why="round 4 (L2): a link row exists only when collect.links' selector judged the target "
            "relevant in its linking context (link-select-v1), so triage fetches it without asking "
            "again; papers still get lookup abstracts where the lookups find them"),
)

# ---------------------------------------------------------------- links (collect.links, round 4 (L))
#: The labs' blog channels of ``collect.blogs`` (posts, not cards): link sources listed but off.
LAB_BLOG_CHANNELS: tuple[str, ...] = (
    "anthropic_alignment_blog", "openai_blog", "openai_alignment_blog", "transformer_circuits",
    "google_research", "mistral_blog", "deepmind_blog",
)

#: Zvi's recurring non-AI series (title substrings, case-insensitive): not linking documents
#: (``newsletter_links``). Observed in the L3 run: "Childhood and Education" (also excluded from
#: ``zvi_posts``/``newsletter_posts``) and "Monthly Roundup", 11 % of Zvi's selector input for 1
#: selection in 151 candidates; added as his other recurring non-AI series: "Housing Roundup",
#: "Medical Roundup", "Fertility Roundup" (round 4 L4). The posts still enter the corpus
#: (``newsletter_posts`` is unchanged).
ZVI_NON_AI_SERIES: tuple[str, ...] = (
    "Childhood and Education", "Monthly Roundup", "Housing Roundup", "Medical Roundup", "Fertility Roundup",
)

#: Which documents are linking documents (``collect.links`` reads their raw HTML bodies). The
#: plan line's ``options.rules`` names the rules applied; default :data:`LINK_RULES_ON`.
LINK_RULES: tuple[SourceRule, ...] = (
    SourceRule(
        "newsletter_links", "links",
        any_of=(Match(channels=NEWSLETTER_CHANNELS),),
        # Zvi's non-AI series only (the other newsletters' titles are not matched)
        exclude=Match(channels=("zvi_substack",), title_keywords=ZVI_NON_AI_SERIES),
        why="2026-10-12: the link sources are Zvi's posts, Import AI (jack-clark.net) and Paradigm3 "
            "news; LessWrong is not a link source for now; round 4 L4: Zvi's non-AI series "
            "(ZVI_NON_AI_SERIES) are not read for links"),
    SourceRule(
        "flagged_links", "links",
        any_of=(Match(keys=(("flag", "collect_links"),)),),
        exclude=None,
        why="2026-10-12: a manual source carries a flag to collect its links "
            "(overrides/manual_sources.yaml, flag `collect_links`)"),
    SourceRule(
        "lab_post_links", "links",
        any_of=(Match(channels=LAB_BLOG_CHANNELS),),
        exclude=None,
        why="link-crawling.md §4.2 (labs' posts as linking documents); listed, off: the 2026-10-12 "
            "link-source list names only the three newsletters"),
    SourceRule(
        "safety_org_links", "links",
        any_of=(Match(channels=SAFETY_ORG_CHANNELS),),
        exclude=None,
        why="link-crawling.md §4.2 (safety-org posts as linking documents); listed, off: not in the "
            "2026-10-12 link-source list"),
)
#: The link rules ``collect.links`` applies when its plan line names none.
LINK_RULES_ON: tuple[str, ...] = ("newsletter_links", "flagged_links")

#: Each linking channel's own publication hosts (``www.``-less; a parent covers its subdomains):
#: a link to them is a self-link, dropped at extraction (older posts, section anchors, subscribe
#: pages). The linking page's own host is always self as well.
LINK_SELF_HOSTS: dict[str, tuple[str, ...]] = {
    "zvi_substack": ("thezvi.substack.com", "thezvi.wordpress.com"),
    "import_ai": ("jack-clark.net", "importai.substack.com"),
    "paradigm3_news": ("paradigm3.org", "p3humansonai.substack.com"),
    # Concordia AI's own reports (concordia-ai.com) are documents, not self-links
    "aisafetychina_substack": ("aisafetychina.substack.com",),
}
#: The content region of a linking **page** by host (an XPath; the first match is the region).
#: A host without an entry: the largest ``article``/``main``/``[role=main]`` element, else
#: ``body``. In-band bodies (feed ``content:encoded``, GraphQL ``htmlBody``) are all content.
LINK_REGIONS: dict[str, str] = {
    # paradigm3.org/news/<newsletter> (Astro): <main><article><header>…</header><div><div class=
    # "article-body">; the header holds the title block, the footer the site links (probe 2026-10-12).
    "paradigm3.org": "//div[contains(concat(' ', normalize-space(@class), ' '), ' article-body ')]",
}
#: Hosts whose links are never a document: CDNs and image hosts (dropped at extraction, ``asset``).
LINK_ASSET_HOSTS: tuple[str, ...] = (
    "substackcdn.com", "substack-post-media.s3.amazonaws.com", "pbs.twimg.com", "video.twimg.com",
    "i.imgur.com", "imgur.com", "images.unsplash.com", "giphy.com", "media.giphy.com", "tenor.com",
)
#: File extensions of asset links (images, fonts, styles), dropped at extraction (``asset``).
LINK_ASSET_EXTENSIONS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".ico",
                                          ".css", ".woff", ".woff2")
#: App stores and app deep links (dropped at extraction, ``app``).
LINK_APP_HOSTS: tuple[str, ...] = ("apps.apple.com", "itunes.apple.com", "play.google.com", "appgallery.huawei.com")
#: Extraction noise by path (any host): reason → regex over the path (case-insensitive).
LINK_NOISE_PATHS: tuple[tuple[str, str], ...] = (
    ("subscribe", r"(?:^|/)(?:subscribe|subscription|account|sign-?in|sign-?up|log-?in|register)(?:/|$)"),
    ("comment", r"(?:^|/)(?:comments?|replies)(?:/|$)"),
    ("app", r"^/app(?:/|$)|^/app-link(?:/|$)"),
)
#: A url-group target whose path is a listing or a site page, not content (rule kind
#: ``navigation``): tag, category, archive, search, login, about, careers, pricing pages, and a
#: bare section index such as ``/blog`` or ``/research``.
LINK_NAVIGATION_PATHS: tuple[str, ...] = (
    r"^/(?:tags?|categor(?:y|ies)|topics?|labels?|archives?|search|authors?|people|team|about(?:-us)?"
    r"|careers?|jobs|pricing|plans|contact(?:-us)?|privacy(?:-policy)?|terms(?:-of-service)?|legal|cookies?"
    r"|login|log-in|signin|sign-in|signup|sign-up|register|subscribe|newsletter|events|press|feed|rss|account"
    r"|settings|donate|shop|store)(?:/|$)",
    r"^/(?:blog|news|research|publications|papers|posts|articles|podcasts?|videos|resources|updates)/?$",
    r"^/page/\d+/?$",
)
#: Link kinds rejected by rule (no LLM call; reason ``rule:<kind>``); the plan line's
#: ``options.exclude_kinds`` replaces the list. ``homepage`` = a url-group target at a site's
#: root (news homepages, government portals, any landing page: it promises links, it is not
#: content); ``navigation`` = :data:`LINK_NAVIGATION_PATHS`; the others are
#: :func:`stack.urls.link_keys` kind hints.
LINK_EXCLUDED_KINDS: tuple[str, ...] = ("homepage", "navigation", "video", "wiki", "shop", "social_profile")

#: Chinese safety stems (appended to :data:`SAFETY_STEMS`): ``(name, regex)``, substrings (Chinese has no
#: word spaces, so no ``\b``). A small curated list of AI-safety compounds from the Chinese-coverage review
#: (its checklist documents, the Concordia database's titles, TC260 and CAICT texts).
ZH_SAFETY_STEMS: tuple[tuple[str, str], ...] = (
    ("zh ai safety", r"人工智能安全|AI安全|大模型安全|模型安全|内容安全|安全对齐|安全评测|安全评估|安全基准|安全测评"),
    ("zh alignment", r"价值对齐|超级对齐|模型对齐|偏好对齐|人类对齐|对齐人类|与人类对齐|对齐研究|对齐技术|对齐方法|未对齐"),
    ("zh jailbreak", r"越狱"),
    ("zh prompt injection", r"提示词?注入"),
    ("zh red-team", r"红队"),
    ("zh interpretability", r"可解释性|机制可解释|机理可解释"),
    ("zh backdoor", r"后门"),
    ("zh poison", r"投毒"),
    ("zh adversarial", r"对抗(?:攻击|样本|训练|鲁棒|性攻击|提示)"),
    ("zh robust", r"鲁棒"),
    ("zh decepti", r"欺骗"),
    ("zh sycophan", r"谄媚|阿谀"),
    ("zh reward hack", r"奖励(?:黑客|欺骗|篡改|作弊|劫持)|规范博弈"),
    ("zh guardrail", r"护栏"),
    ("zh refusal", r"拒答|拒绝回答"),
    ("zh misuse", r"滥用"),
    ("zh frontier risk", r"灾难性风险|生存风险|存在性风险|前沿风险|失控"),
    ("zh unlearning", r"机器遗忘|知识遗忘|遗忘学习|机器反学习"),
    ("zh watermark", r"水印"),
    ("zh oversight", r"可扩展监督|弱到强"),
    ("zh harmful content", r"有害内容|有害输出|无害性"),
    ("zh honesty", r"诚实性"),
)
#: Safety stems: ``(name, regex)``, case-insensitive, matched anywhere (module docstring, ``collect``).
#: The relevance proxy of :mod:`stack.evals.freeze` (``kw_high``) and the collection-yield
#: measurements (docs/design/collection-yield.md §1) read the same list; editing it re-scopes the
#: collectors' filters and the proxy together.
SAFETY_STEMS: tuple[tuple[str, str], ...] = (
    ("alignment", r"\balignment\b|\baligned (ai|model|llm)"),
    ("superalignment", r"\bsuperalign"),
    ("misalign", r"\bmisalign"),
    ("safety", r"\bsafety\b|\bai[- ]safe"),
    ("interpretab", r"\binterpretab"),
    ("mechanistic", r"\bmechanistic"),
    ("red-team", r"\bred[- ]?team"),
    ("jailbreak", r"\bjailbreak"),
    ("prompt injection", r"\bprompt[- ]injection"),
    ("oversight", r"\boversight\b"),
    ("rlhf", r"\brlhf\b|reinforcement learning from human feedback"),
    ("reward hack", r"\breward[- ]hack|\breward tampering|\bspecification gaming"),
    ("decepti", r"\bdecepti|\bdeceiv"),
    ("schem", r"\bschem(e|es|ing|er|ers)\b"),
    ("sandbag", r"\bsandbag"),
    ("sycophan", r"\bsycophan"),
    ("misuse", r"\bmisuse"),
    ("guardrail", r"\bguardrail"),
    ("refusal", r"\brefus(al|als|e|es|ing)\b"),
    ("constitutional", r"\bconstitutional"),
    ("monitor", r"\bmonitor"),
    ("ai control", r"\bai control\b|\bcontrol (evaluation|protocol)s?\b"),
    ("dangerous capabilit", r"\bdangerous[- ]capabilit"),
    ("frontier risk", r"\bfrontier (ai )?risk|\bcatastrophic risk|\bexistential risk|\bx-risk"),
    ("preparedness", r"\bpreparedness"),
    ("responsible scaling", r"\bresponsible scaling"),
    ("unlearning", r"\bunlearn"),
    ("backdoor", r"\bbackdoor"),
    ("poison", r"\bpoison"),
    ("watermark", r"\bwatermark"),
    ("adversarial", r"\badversarial"),
    ("robust", r"\brobust"),
    ("corrigib", r"\bcorrigib"),
    ("steering", r"\bsteering\b"),
    ("model organism", r"\bmodel organism"),
    ("power-seeking", r"\bpower[- ]seeking"),
    ("situational awareness", r"\bsituational awareness|\bevaluation awareness"),
    # Chinese (the Chinese-coverage work): written without spaces, so matched as substrings, no `\b`; AI-safety compounds rather
    # than the bare words (安全 is also "security", 对齐 typesetting and sequence alignment, 监督 supervised learning).
    *ZH_SAFETY_STEMS,
)


def safety_stem_regex() -> str:
    """One regex matching any :data:`SAFETY_STEMS` entry (apply case-insensitively), for a
    collector's configured ``title_include_re``."""
    return "|".join(f"(?:{p})" for _, p in SAFETY_STEMS)


#: ``re.ASCII``: ``\b`` and ``\w`` are ASCII-only, so an English stem next to a Chinese character still has a word
#: boundary ("LLM安全", "AI对齐"); without it a CJK character is a word character and ``\bllm`` never matches there.
_SAFETY_STEM_RE = re.compile(safety_stem_regex(), re.IGNORECASE | re.ASCII)


def has_safety_stem(text: str | None) -> bool:
    """Whether ``text`` contains a safety stem (:data:`SAFETY_STEMS`, case-insensitive)."""
    return bool(text) and _SAFETY_STEM_RE.search(text) is not None


#: Pre-2020 safety stems (D654): ``(name, regex)``, case-insensitive, matched anywhere in a keyword
#: hit's title and abstract. The period's AI-safety vocabulary, phrase-level and AI-specific: :data:`SAFETY_STEMS` is
#: modern (``\bsafety\b`` admits every "patient safety" paper and misses "safe", "safer", "superintelligence"). A
#: hit published before ``keyword_cuts.publisher_cut_floor`` with one of these bypasses the source cuts
#: (:func:`publisher_cut_exempt`). Replayed on the R2 C11 sample (800 OpenAlex hits of 2015-2019, full abstracts):
#: 13 of the 16 labelled-relevant cut hits and 1 of 152 labelled-irrelevant ones pass. Not here on purpose: "impact
#: measure" (research-evaluation and clinical "impact measures"), "negative side effects" (pharmacology),
#: "power-seeking" (political science), "reward model" (neuroscience). Editing the list changes
#: :func:`pre2020_stem_digest`, which is in the coverage key of every group with the floor configured.
PRE2020_SAFETY_STEMS: tuple[tuple[str, str], ...] = (
    ("ai safety", r"\b(ai|agi|a\.i\.)[- ]safe(ty)?\b|\bsafe (ai|agi)\b|\bsafety of (ai|agi|artificial)"
                  r"|\bartificial (general )?intelligence safety"),
    ("ai alignment", r"\b(ai|agi) alignment\b|\bvalue[- ]align|\baligned (ai|agi)\b"),
    ("ai risk", r"\b(ai|agi) risks?\b|\bexistential (risk|threat|catastrophe)|\bcatastrophic risks?\b|\bx[- ]risk"),
    ("superintelligence", r"\bsuper[- ]?intelligen"),
    ("intelligence explosion", r"\bintelligence explosion|\btechnological singularity"),
    ("friendly ai", r"\bfriendly (ai|agi|artificial)"),
    ("ai containment", r"\b(ai|agi) (control problem|boxing|box|containment|confinement)\b|\bcontainment problem\b"),
    ("oracle ai", r"\boracle (ai|agi)\b|\b(ai|agi) oracles?\b"),
    ("corrigib", r"\bcorrigib"),
    ("interruptib", r"\bsafe(ly)?[- ]interruptib"),
    ("wirehead", r"\bwire[- ]?head"),
    ("quantiliz", r"\bquantili[sz]"),
    ("reward corruption", r"\breward (hacking|tampering|corruption)|\bcorrupted reward|\bspecification gaming"
                          r"|\b(reward|utility) function (integrity|security)"),
    ("safe rl", r"\bsafe (deep )?(reinforcement learning|rl\b|policy (search|improvement|optimi[sz]ation))|\bsaferl\b"),
    ("reward design", r"\binverse reward design|\boff[- ]switch game"),
    ("machine ethics", r"\bmachine ethics|\bartificial moral agen|\bmoral machines?\b"),
    ("agent foundations", r"\bmesa[- ]optimi|\binner alignment|\bembedded agen|\butility indifference"
                          r"|\binstrumental convergence|\bbasic ai drives"),
)
#: Pre-2020 stems that count only with a learning-context term (:data:`PRE2020_LEARNING_CONTEXT`) in the same text:
#: "safe exploration" is also oil drilling and crewed spaceflight (both in the R2 sample).
PRE2020_CONTEXT_STEMS: tuple[tuple[str, str], ...] = (
    ("safe exploration", r"\bsafe exploration\b|\bsafe (bayesian )?optimi[sz]ation"),
)
PRE2020_LEARNING_CONTEXT: str = (r"\b(learn\w*|bandits?|reinforcement|agents?|polic(y|ies)|markov|gaussian process\w*"
                                 r"|bayesian optimi[sz]ation)\b")

_PRE2020_RE = re.compile("|".join(f"(?:{p})" for _, p in PRE2020_SAFETY_STEMS), re.IGNORECASE)
_PRE2020_CONTEXT_RE = re.compile("|".join(f"(?:{p})" for _, p in PRE2020_CONTEXT_STEMS), re.IGNORECASE)
_PRE2020_LEARNING_RE = re.compile(PRE2020_LEARNING_CONTEXT, re.IGNORECASE)


def has_pre2020_safety_stem(text: str | None) -> bool:
    """Whether ``text`` carries a pre-2020 safety stem (:data:`PRE2020_SAFETY_STEMS`, or a
    :data:`PRE2020_CONTEXT_STEMS` entry together with :data:`PRE2020_LEARNING_CONTEXT`)."""
    if not text:
        return False
    return _PRE2020_RE.search(text) is not None or (
        _PRE2020_CONTEXT_RE.search(text) is not None and _PRE2020_LEARNING_RE.search(text) is not None)


def pre2020_safety_stems_in(text: str | None) -> list[str]:
    """The names of the pre-2020 stems ``text`` carries (diagnostics and replays)."""
    if not text:
        return []
    found = [n for n, p in PRE2020_SAFETY_STEMS if re.search(p, text, re.IGNORECASE)]
    if _PRE2020_LEARNING_RE.search(text):
        found += [n for n, p in PRE2020_CONTEXT_STEMS if re.search(p, text, re.IGNORECASE)]
    return found


def pre2020_stem_digest() -> str:
    """12 hex characters of the SHA-256 of the pre-2020 stem lists and the learning context: the coverage key's
    record of the stem set (a changed list is a new key, so the group re-sweeps from the floor)."""
    blob = json.dumps([PRE2020_SAFETY_STEMS, PRE2020_CONTEXT_STEMS, PRE2020_LEARNING_CONTEXT], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def publisher_cut_exempt(text: str | None, published: str | None, floor: str | None) -> bool:
    """Whether a keyword hit is exempt from the source cuts (D654): a ``floor`` is configured
    (``keyword_cuts.publisher_cut_floor``, an ISO date), the hit's ``published`` date (``YYYY-MM-DD``, or a bare
    ``YYYY`` read as its 1 January) is before it, and its title and abstract carry a pre-2020 safety stem
    (:func:`has_pre2020_safety_stem`). An undated hit is not exempt (it cannot be shown to be pre-floor)."""
    if not floor or not published:
        return False
    day = str(published).strip()[:10]
    if re.fullmatch(r"\d{4}", day):
        day = f"{day}-01-01"
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return False
    return day < floor and has_pre2020_safety_stem(text)


# ── collect: the backfill cuts (2026-10-13; module docstring, docs/design/collection-yield.md §10) ─────────────
#: AI terms: ``(name, regex)``, matched case-insensitively over a normalised title + abstract (lower case; hyphens,
#: dashes, underscores and slashes as spaces; whitespace collapsed), plus :data:`AI_TERMS_UPPER` case-sensitively
#: over the raw text. Cut ``ai_term`` keeps a hit of a listed concept group only when one matches: the unquoted
#: token-AND groups otherwise return oncology, materials and pavement mechanics (16-backfill-yield §2.2). The list is
#: the regex the 2026-10-13 simulation used (``sim2.py``), split into named entries.
AI_TERMS: tuple[tuple[str, str], ...] = (
    ("artificial intelligence", r"\bartificial intelligence\b"),
    ("language model", r"\blanguage models?\b|\bllms?\b|\bvlms?\b|\bmllms?\b|\blarge models?\b"),
    ("foundation/frontier model", r"\bfoundation models?\b|\bfrontier models?\b"),
    ("neural network", r"\bneural networks?\b|\bneural\b|\bdeep (neural|models?)\b"),
    ("deep/machine/reinforcement learning", r"\bdeep learning\b|\bmachine learning\b|\breinforcement learning\b"),
    ("transformer", r"\btransformers?\b"),
    ("gpt, chatbot", r"\bgpt\b|\bchatgpt\b|\bchatbots?\b"),
    ("generative ai", r"\bgenerative (ai|models?)\b"),
    ("ai system", r"\bai (agents?|systems?|models?|safety|alignment)\b"),
    ("agent", r"\bllm agents?\b|\blanguage agents?\b|\bagentic\b|\bautonomous agents?\b"),
    ("multimodal", r"\bvision language\b|\bmultimodal models?\b|\bdiffusion models?\b"),
    ("training", r"\brlhf\b|\bfine tun\w*|\bpretrain\w*"),
    ("sparse autoencoder", r"\bsparse autoencoders?\b"),
)
#: Upper-case abbreviations, matched case-sensitively over the raw title + abstract ("AI", "A.I.", "AGI").
AI_TERMS_UPPER: str = r"\bAI\b|\bA\.I\.|\bAGI\b"
#: Chinese AI terms (the Chinese-coverage work): ``(name, regex)``, substrings over the raw title + abstract (no word spaces, no
#: ``\b``). A hit with one passes the ``ai_term`` cut as an English AI term does.
AI_TERMS_ZH: tuple[tuple[str, str], ...] = (
    ("zh artificial intelligence", r"人工智能|生成式AI|AIGC"),
    ("zh language model", r"大模型|大语言模型|语言模型|基础模型|基座模型|预训练模型|多模态模型"),
    ("zh neural network", r"神经网络"),
    ("zh deep/machine/reinforcement learning", r"深度学习|机器学习|强化学习"),
    ("zh generative model", r"生成式模型|生成模型|扩散模型"),
    ("zh agent", r"智能体"),
    ("zh chatbot", r"聊天机器人|对话模型"),
    ("zh training", r"微调|预训练|人类反馈"),
    ("zh multimodal", r"多模态|视觉语言"),
)

#: ``re.ASCII`` (as ``_SAFETY_STEM_RE``): an English term next to a Chinese character keeps its word boundary.
_AI_TERM_RE = re.compile("|".join(f"(?:{p})" for _, p in AI_TERMS), re.ASCII)
_AI_UPPER_RE = re.compile(AI_TERMS_UPPER, re.ASCII)
_AI_ZH_RE = re.compile("|".join(f"(?:{p})" for _, p in AI_TERMS_ZH))


def _norm_ai_text(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[-‐–_/]", " ", text.lower()))


def ai_term_digest() -> str:
    """12 hex characters of the SHA-256 of the AI-term vocabulary (:data:`AI_TERMS`, :data:`AI_TERMS_UPPER`,
    :data:`AI_TERMS_ZH`): in the coverage key of every ``ai_term`` group (D751), so an edited vocabulary re-sweeps
    those groups and recovers the hits the old one dropped."""
    blob = json.dumps([AI_TERMS, AI_TERMS_UPPER, AI_TERMS_ZH], ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def has_ai_term(text: str | None) -> bool:
    """Whether ``text`` (title and abstract) carries an AI term (:data:`AI_TERMS`, :data:`AI_TERMS_UPPER`,
    :data:`AI_TERMS_ZH`)."""
    if not text:
        return False
    return (_AI_TERM_RE.search(_norm_ai_text(text)) is not None or _AI_UPPER_RE.search(text) is not None
            or (not text.isascii() and _AI_ZH_RE.search(text) is not None))


#: DOI registrant prefixes of self-publication and preprint servers. Cut ``self_publication`` drops a keyword-family
#: hit whose only location is one of them (16-backfill-yield §2.1: 28 % of the week's paper documents, mostly
#: self-published work of low quality; none of SR 2025's 760 inclusions came from Zenodo).
SELF_PUBLICATION_DOI_PREFIXES: tuple[str, ...] = (
    "10.5281",   # Zenodo
    "10.21203",  # Research Square
    "10.20944",  # Preprints.org
    "10.17605",  # OSF (DataCite)
    "10.31219",  # OSF Preprints
    "10.31234",  # PsyArXiv (OSF)
    "10.31235",  # SocArXiv (OSF)
    "10.6084",   # figshare
    "10.36227",  # TechRxiv
    "10.22541",  # Authorea
    "10.32388",  # Qeios
    "10.1101",   # bioRxiv / medRxiv (also Cold Spring Harbor journals: both dropped with cut `publisher` on)
    "10.64898",  # bioRxiv (newer registrant, seen in the 2026-10 rehearsal)
)
#: Preprint-server prefixes kept like arXiv: SSRN (SR 2025 included 2 SSRN papers; 16-backfill-yield §2.1 note 1).
KEPT_PREPRINT_DOI_PREFIXES: tuple[str, ...] = ("10.2139",)
#: DOI registrant prefixes of peer-reviewed venues whose concept-group hits the ``publisher`` cut keeps when the
#: channel sets ``keyword_cuts.keep_venues`` (approved 2026-10-30; source class ``venue``): venues whose
#: papers acquire's oa lane fetches and that the cut dropped as publisher works. Measured on the store export copy
#: (documents without an arXiv key, oa-lane full-text attempts that ended ok): ACL Anthology 418 of 426 (98 %),
#: AAAI 125 of 140 (89 %), IJCAI 35 of 38, NDSS 9 of 9, PLOS 7 of 7; for comparison IEEE 25 of 382, ACM 44 of 545,
#: Springer 13 of 553. Editing the list
#: changes :func:`venue_allowlist_digest`, which is in the coverage key of every group the cut applies to.
VENUE_DOI_PREFIXES: tuple[tuple[str, str], ...] = (
    ("10.18653", "ACL Anthology (ACL, EMNLP, NAACL, EACL, COLING, Findings, workshops)"),
    ("10.1609", "AAAI (AAAI conference, AIES, ICWSM, AAAI symposia)"),
    # added 2026-10-30 (D753): oa-lane full text 35 of 38, 9 of 9, 7 of 7
    ("10.24963", "IJCAI proceedings"),
    ("10.14722", "NDSS Symposium (Internet Society)"),
    ("10.1371", "PLOS journals"),
)
#: OpenAlex source ids of the arXiv repository (``locations[].source.id``): "arXiv (Cornell University)".
OPENALEX_ARXIV_SOURCE_IDS: tuple[str, ...] = ("S4306400194",)
#: Semantic Scholar ``venue`` strings (lower case) that mean an arXiv deposit.
S2_ARXIV_VENUES: tuple[str, ...] = ("arxiv", "arxiv.org")

#: The keyword-family cuts, in the order :func:`keyword_cut` tests them (the first that applies is counted).
KEYWORD_CUTS: tuple[str, ...] = ("self_publication", "publisher", "ai_term")
#: The source cuts a channel may list in ``keyword_cuts.drop_sources``, by the source classes each drops.
SOURCE_CUT_CLASSES: dict[str, tuple[str, ...]] = {
    "publisher": ("publisher", "no_doi"),
    "self_publication": ("self_publication",),
}


def venue_allowlist_digest() -> str:
    """12 hex characters of the SHA-256 of :data:`VENUE_DOI_PREFIXES`' prefixes: the coverage key's record of the
    allowlist (a changed list is a new key, so the group re-sweeps from the floor)."""
    blob = json.dumps(sorted(p for p, _ in VENUE_DOI_PREFIXES))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def venue_allowlist_applies(text: str | None, *, keep_venues: bool, venue_ai_term: bool = False) -> bool:
    """Whether the venue allowlist (``keep_venues``) applies to a hit whose title and abstract are ``text``: with
    ``venue_ai_term`` (``keyword_cuts.venue_ai_term``) only when ``text`` has an AI term (:func:`has_ai_term`), so a
    broad venue's non-AI paper (a PLOS biomedical article a broad group's phrase finds) stays a ``publisher`` work."""
    return keep_venues and (not venue_ai_term or has_ai_term(text))


def source_class(keys: Iterable[tuple[str, str]], *, arxiv_located: bool = False, keep_venues: bool = False) -> str:
    """Where a scholarly hit lives, from its typed keys: ``arxiv`` (an ``arxiv`` key, or ``arxiv_located``: an arXiv
    location the collector saw in the record), ``kept_preprint`` (:data:`KEPT_PREPRINT_DOI_PREFIXES`), ``venue``
    (``keep_venues`` and a DOI of :data:`VENUE_DOI_PREFIXES`), ``self_publication`` (a ``zenodo`` key or
    :data:`SELF_PUBLICATION_DOI_PREFIXES`), ``publisher`` (any other DOI), ``no_doi`` (neither). DOIs are in the
    ``doi`` key's normal form (lower case, ``10.NNNN/…``). No cut drops ``venue``."""
    pairs = list(keys)
    if arxiv_located or any(k == "arxiv" for k, _ in pairs):
        return "arxiv"
    prefixes = [v.split("/", 1)[0] for k, v in pairs if k == "doi"]
    if any(p in KEPT_PREPRINT_DOI_PREFIXES for p in prefixes):
        return "kept_preprint"
    if keep_venues and any(p in {v for v, _ in VENUE_DOI_PREFIXES} for p in prefixes):
        return "venue"
    if any(k == "zenodo" for k, _ in pairs) or any(p in SELF_PUBLICATION_DOI_PREFIXES for p in prefixes):
        return "self_publication"
    return "publisher" if prefixes else "no_doi"


def source_cut(keys: Iterable[tuple[str, str]], *, drop_sources: Iterable[str] = (),
               arxiv_located: bool = False, keep_venues: bool = False) -> str | None:
    """The source cut in ``drop_sources`` whose classes (:data:`SOURCE_CUT_CLASSES`) include the hit's
    :func:`source_class` (with ``keep_venues``), else ``None``. An unknown cut name is a :class:`RuleError`."""
    drops = tuple(drop_sources)
    unknown = sorted(set(drops) - set(SOURCE_CUT_CLASSES))
    if unknown:
        raise RuleError(f"unknown source cut(s) {unknown} (known: {sorted(SOURCE_CUT_CLASSES)})")
    if drops:
        cls = source_class(keys, arxiv_located=arxiv_located, keep_venues=keep_venues)
        for cut in KEYWORD_CUTS:
            if cut in drops and cls in SOURCE_CUT_CLASSES[cut]:
                return cut
    return None


def keyword_cut(keys: Iterable[tuple[str, str]], text: str | None, *, drop_sources: Iterable[str] = (),
                ai_term: bool = False, arxiv_located: bool = False, published: str | None = None,
                publisher_cut_floor: str | None = None, keep_venues: bool = False,
                venue_ai_term: bool = False) -> str | None:
    """The cut that drops a keyword-family hit, else ``None``: the :func:`source_cut` (a ``keep_venues`` hit of an
    allowlisted venue has none; with ``venue_ai_term`` only one with an AI term, :func:`venue_allowlist_applies`),
    unless the hit is :func:`publisher_cut_exempt` (published before
    ``publisher_cut_floor`` with a pre-2020 safety stem; D654), else ``ai_term`` when ``ai_term`` is set and ``text``
    (title and abstract) has no :func:`has_ai_term` (an exempt or venue hit too). An unknown cut name is a
    :class:`RuleError`."""
    keep_venues = venue_allowlist_applies(text, keep_venues=keep_venues, venue_ai_term=venue_ai_term)
    cut = source_cut(keys, drop_sources=drop_sources, arxiv_located=arxiv_located, keep_venues=keep_venues)
    if cut is not None and not publisher_cut_exempt(text, published, publisher_cut_floor):
        return cut
    if ai_term and not has_ai_term(text):
        return "ai_term"
    return None


#: Every source rule, by the stage that reads it (module docstring).
RULES: dict[str, tuple[SourceRule, ...]] = {
    "triage": TRIAGE_RULES,
    "links": LINK_RULES,
}


def rule(name: str, stage: str) -> SourceRule:
    """The rule ``name`` of ``stage``; :class:`RuleError` when it is unknown, belongs to another
    stage, or the name is defined twice."""
    found = [r for rs in RULES.values() for r in rs if r.name == name]
    if len(found) > 1:
        raise RuleError(f"source rule {name!r} is defined {len(found)} times in stack/rules.py")
    if not found:
        known = sorted(r.name for r in RULES.get(stage, ()))
        raise RuleError(f"unknown source rule {name!r} (the {stage} rules: {known})")
    r = found[0]
    if r.stage != stage:
        raise RuleError(f"source rule {name!r} is a {r.stage} rule, not a {stage} rule")
    return r


__all__ = ["AI_TERMS", "AI_TERMS_UPPER", "AI_TERMS_ZH", "ZH_SAFETY_STEMS", "LAB_BLOG_CHANNELS", "LAB_CHANNELS", "LAB_URL_PREFIXES", "LINK_APP_HOSTS", "LINK_ASSET_EXTENSIONS",
           "LINK_ASSET_HOSTS", "LINK_EXCLUDED_KINDS", "LINK_NAVIGATION_PATHS", "LINK_NOISE_PATHS", "LINK_REGIONS",
           "LINK_RULES", "LINK_RULES_ON", "LINK_SELF_HOSTS", "NEWSLETTER_CHANNELS", "RULES", "SAFETY_ORG_CHANNELS",
           "SAFETY_STEMS", "TRIAGE_RULES", "Match", "RuleError", "SourceRule", "has_safety_stem", "rule",
           "safety_stem_regex", "sql_literal",
           "KEPT_PREPRINT_DOI_PREFIXES", "KEYWORD_CUTS", "OPENALEX_ARXIV_SOURCE_IDS", "S2_ARXIV_VENUES",
           "SELF_PUBLICATION_DOI_PREFIXES", "SOURCE_CUT_CLASSES", "has_ai_term", "keyword_cut", "source_class",
           "venue_allowlist_applies",
           "PRE2020_CONTEXT_STEMS", "PRE2020_LEARNING_CONTEXT", "PRE2020_SAFETY_STEMS", "has_pre2020_safety_stem",
           "pre2020_safety_stems_in", "pre2020_stem_digest", "publisher_cut_exempt", "source_cut",
           "VENUE_DOI_PREFIXES", "venue_allowlist_digest", "ai_term_digest"]
