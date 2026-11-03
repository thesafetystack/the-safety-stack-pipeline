# Future: postponed work

Work that is postponed or open, with the code paths it touches. The code paths named here are kept and tested unless
stated. Open choices recorded as decisions are the `docs/DECISIONS.md` rows marked "default taken". Checked against
the code on 2026-11-03.

- **LessWrong comments.** Postponed. No collector stores comments today: `collect.lw_graphql` asks only for a post's
  `commentCount`, and no family has a place for a comment. Collecting them needs a decision on what a comment is (a
  document of its own, or part of its post's text) before any code.
- **Newsletter links.** `collect.feeds`, `collect.manual` and `collect.links` run in the production plan
  (`batch-2026-10a`, D658, D664), but `collect.links` harvests only the links of the manual sources flagged
  `collect_links` (`options: {rules: [flagged_links]}`), not the newsletters' links (design
  `docs/design/link-crawling.md`; `links-scratch.yaml` is the working template).
- **X hydration.** Postponed (no X thread import for now). `collect.x-lookup` (a self-thread as one document keyed by
  `(conversation_id, author)`, D243-D247) is implemented and tested on fakes and a scratch store, and is in no plan;
  its plan line is in `workflows/collect/x-lookup/README.md`. Full-archive search (`/2/tweets/search/all`) works on
  the current API tier (D247), so threads older than a week can be searched.

## Egress and fetch safety (D651, D680)

- **Egress filtering.** Since D680 the only SSRF check is the URL a fetch starts at; redirect hops, links in pages,
  DNS rebinding, fetchpdf's own requests and everything a Chromium page asks for are unchecked, which is accepted on a
  host with no vulnerable ports reachable. The setting for a later boundary exists and is off: the standard
  `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` variables, which urllib, `requests` (the OA lane, fetchpdf) and Chromium
  read natively (`docs/SETUP.md` §2). The later step, if the host changes or a plan enables broader browser navigation
  (`oa_playwright`, the http lane's `headless`): a filtering proxy behind those variables denying interior ranges, or
  a network namespace or firewall for the process (for Chromium too; `--host-resolver-rules` covers names, not
  literals). Not decided.
- **Unpaywall's per-location transients** (D651): `_unpaywall` re-raises every `_pdf` verdict as `inconclusive`, so a
  403/5xx/timeout on an OA location is not recorded as a transient; with no artifact the role ends
  `no_fetchable_url` for 90 days instead of `failed_transient`. Fix: record `got.transient[step]` before re-raising.
  It moves non-SSRF outcomes too (every Unpaywall-location 403 becomes `failed_transient[forbidden]` until the cap),
  so it waits for a decision. Open.
- **NAT64-only hosts** (D650): the start-URL check refuses NAT64 and 6to4 addresses as a class; on a DNS64/NAT64-only
  box every publisher resolved through the check's resolver would be `blocked`. Allowing NAT64-of-public would need
  the classifier (`stack.http._addr_is_interior`) to accept that form. Open.

## Other open items

Open choices also recorded as decision rows are not repeated (D291-D293, D303).

- **`--passes` with keyless Semantic Scholar never converges.** A pass converges only when every run of it made no
  call and changed no row (`stack/build.py` `pass_converged`), `require_complete: false` lines included, and keyless
  `collect.s2-lookup` makes calls in every pass; so passes run to N. Options: an S2 key, or let convergence ignore
  exempt lines. Open.
- **The arXiv author family is off** in the production plan, as it was in `std-v3` (`collect.arxiv_sweep`
  `families: [kw, cat]`): on `std-v3` it was 2,242 author queries, ~2 h nominal and ~8 h once arXiv answers 429. Open.
- **Affiliation's work key includes the source id** (D19): a document whose anchor member changes is asked again
  with nothing else changed. Open.
- **A cancelled run publishes nothing**: a drained run (a first SIGTERM or `stack stop`, D570) publishes what it
  completed, but a hard cancel (a second SIGTERM, SIGINT, `stack stop --now`) ends `cancelled[operator]` and its
  outputs, written at a run's end only (`docs/OPERATE.md`, "A build"), are lost. Open; by design today.
- **pmc.ncbi.nlm.nih.gov answered 403** to every OA-lane request in a 2026-10 test build (324 of 1,791 OA requests).
  Not acted on; not re-checked.
- **Zenodo pacing bounds the OA lane** (2.1 s between requests, guest limit 2,000/h): ~13 h of 15-23 h of OA fetch
  in a 2026-10 test build where Zenodo had a 29 % share. Open.
- **Gate standings refuse an unreadable superseded `documents` file** (`stack/gate.py` `standings`, through the one
  read path of D298). Every file in the stores of 2026-10-13 read, so nothing changed then. Default: refuse. Open for
  confirmation.
