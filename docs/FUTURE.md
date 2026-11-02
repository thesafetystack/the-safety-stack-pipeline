# Future: postponed work

Work postponed by the owner (Tomáš) on 2026-10-13; the code paths named here are kept and tested unless stated.
Other open choices are the `docs/DECISIONS.md` rows marked "default taken, for Tomáš". Moved here from the M1-M5 plan's
§9 (the M1-M5 plan) on 2026-10-14; the list was last checked against the code on 2026-10-13, the link-collection
item on 2026-11-02.

- **LessWrong comments.** Postponed (not now; kept for later). No collector stores comments today:
  `collect.lw_graphql` asks only for a post's `commentCount`, and no family has a place for a comment. Collecting
  them needs a decision on what a comment is (a document of its own, or part of its post's text) before any code.
- **Link crawling and link collection.** In production since plan `batch-2026-10a` revision 5 (D658, D664):
  `collect.feeds`, `collect.manual` and `collect.links` run in every build, with the gate rules `newsletter_posts`,
  `manual_accept` and `selected_links`. Still off: `collect.links` harvests only the links of the manual sources
  flagged `collect_links` (`options: {rules: [flagged_links]}`), not the newsletters' links (design
  `docs/design/link-crawling.md`; `links-scratch.yaml` is the working template), and `collect.x-lookup` (below) is in
  no production plan.
- **X hydration.** Postponed (no X thread import for now). `collect.x-lookup` (a self-thread as one document
  keyed by `(conversation_id, author)`, D243-D247) is implemented and tested on fakes and a scratch store; its plan
  line is in `workflows/collect/x-lookup/README.md`. Full-archive search (`/2/tweets/search/all`) works on the
  current API tier (D247), so threads older than a week can be searched.
- **`same_as` links between documents.** Not to be built: replaced by alternate keys (storing alternate URLs is
  simpler than a dedicated `same_as` relation). Implemented as the key adders and matchers of
  `stack/identity.py` (T-ID, D251-D253; `docs/howto/duplicates-and-urls.md`).

## Egress, and open items from the review-2 SSRF follow-up (D651, D680)

- **Egress filtering.** Since D680 the only SSRF check is the URL a fetch starts at; redirect hops, links in pages,
  DNS rebinding, fetchpdf's own requests and everything a Chromium page asks for are unchecked, which is accepted on a
  host with no vulnerable ports reachable. The setting for a later boundary exists and is off: the standard
  `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` variables, which urllib, `requests` (the OA lane, fetchpdf) and Chromium
  read natively (`docs/SETUP.md` §2). The later step, if the host changes or a plan enables broader browser navigation
  (`oa_playwright`, the http lane's `headless`): a filtering proxy behind those variables denying interior ranges, or
  a network namespace or firewall for the process (for Chromium too; `--host-resolver-rules` covers names, not
  literals). Decision for Tomáš.
- **Unpaywall's per-location transients** (D651, review-2 S3): `_unpaywall` re-raises every `_pdf` verdict as
  `inconclusive`, so a 403/5xx/timeout on an OA location is not recorded as a transient; with no artifact the role
  ends `no_fetchable_url` for 90 days instead of `failed_transient`.
  Fix: record `got.transient[step]` before re-raising. It moves non-SSRF outcomes too (every Unpaywall-location
  403 becomes `failed_transient[forbidden]` until the cap), so it waits for a decision. Open.
- **NAT64-only hosts** (D650): the start-URL check refuses NAT64 and 6to4 addresses as a class; on a DNS64/NAT64-only
  box every publisher resolved through the check's resolver would be `blocked`. Allowing NAT64-of-public would need
  the classifier (`stack.http._addr_is_interior`) to accept that form. Open.

## Open items recorded by the 2026-10-13 rounds

Notes the rehearsal (T-REH), round-6 (R6) and family-format (T-FMT) implementation boards held and no live
doc did; status as of 2026-10-14. Open choices those rounds also recorded as DECISIONS rows are not repeated
(D291-D293, D303).

- **`--passes` with keyless Semantic Scholar never converges.** A pass converges only when every run of it made no
  call and changed no row (`stack/build.py` `pass_converged`), `require_complete: false` lines included, and keyless
  `collect.s2-lookup` made calls in every pass of the rehearsal; so passes run to N. Options: an S2 key, or let convergence ignore
  exempt lines. Open.
- **The arXiv author family is off in std-v3** (`families: [kw, cat]`): 2,242 author queries, ~2 h nominal and ~8 h
  once arXiv answers 429. Open.
- **Affiliation's work key includes the source id** (D19): a document whose anchor member changes is asked again
  with nothing else changed (2 of 11 pass-2 re-asks in the rehearsal). Open.
- **A cancelled run publishes nothing**: outputs are written at a run's end only (`docs/OPERATE.md`, "A build"), so a
  collector cancelled after 131 `ok` windows kept none of them. Open; by design today.
- **pmc.ncbi.nlm.nih.gov answered 403** to every OA-lane request in the rehearsal (324 of 1,791 OA requests). Not
  acted on; not re-checked.
- **Zenodo pacing bounds the OA lane** (2.1 s between requests, guest limit 2,000/h): ~13 h of 15-23 h of OA fetch at
  the rehearsal's 29 % Zenodo share. Open.
- **Gate standings refuse an unreadable superseded `documents` file** (`stack/gate.py` `standings`, through the one
  read path of D298); before T-FMT it read any file raw. Every file in the stores of 2026-10-13 reads, so nothing
  changed then. Default: refuse. Open for confirmation.
- Superseded: a keyless S2 429 keeping every std-v3 build `incomplete` (std-v3's `collect.s2-lookup` has
  `require_complete: false`); the OA lane as one sequential host group (now `fetch_workers - 2` slots,
  `docs/howto/concurrency.md`); arXiv HTML only for `arxiv`-channel members (`structured.arxiv_html` covers every member
  with an arXiv id, D294); an S2 429 inside the OA walk halting the whole lane (S2 is one rung, the lane goes on,
  D402, D413; it halts only on Unpaywall and the ID Converter, D164); the v3 archive importing landing pages as PDFs
  (the archive step is removed, D329).
