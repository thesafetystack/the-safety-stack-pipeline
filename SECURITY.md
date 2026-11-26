# Security

**Reporting a vulnerability.** Email info@thesafetystack.ai: what you found, how to reproduce it, and the commit or
release it applies to. Do not open a public issue for it. If GitHub's private vulnerability reporting is enabled for
this repository, a report there reaches the same people.

**In scope.** The code in this repository and its production cycle (`tools/scheduled_run.py cycle`,
`docs/DEPLOY-railway.md`): the handling of keys and of the store, the release export and its disclosure checks, and
the fetcher's network guard (`stack/http.py` `check_url_safety`, the headless rung in `stack/browser.py`). The guard's
known open gaps are listed in `docs/FUTURE.md` ("Egress and fetch safety"); a report that one of them can be
exploited in practice is in scope, as is any gap not listed there.

**Out of scope.** The dataset's content: a wrong annotation, a document or a name to remove, or personal data to
correct goes through the data repository's correction and takedown route (`data-repo/CONTRIBUTING.md`: its issue
forms, or info@thesafetystack.ai for a private request). Vulnerabilities in the services the pipeline calls (language
model providers, Hugging Face, the document sources) go to those services.

**What to expect.** An acknowledgement of the report, then what will be done about it once it is assessed. A fix
lands as an ordinary commit (with a `docs/DECISIONS.md` row when it changes behaviour) and reaches production when
the maintainers move the deploy branch to it (`docs/DEPLOY-railway.md`). There is no bug bounty.
