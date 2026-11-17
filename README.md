# Incident Learning

**Learn from software failures, not just headlines.** Short summaries lead into
source-backed explanations, engineering lessons, and reflection questions.

**[Open the learning notebook](https://vikhyatcode.github.io/incident-learning/)**

The public, static site covers both outages/system failures and security
incidents/data leaks. Three originally written historical studies (GitHub 2018,
Cloudflare 2019, and LastPass 2022) provide deeper explanations. A daily collector
adds selected current reports from real public feeds. The latest selected news
also has original, source-reviewed explanations, including the Optimizely,
Barracuda, Microsoft 365, Fandango, and FaceStealer reports collected in November
2026. No login, paid service,
API key, or external secret is required.

## Daily operation

`.github/workflows/daily.yml` runs **every day at 08:37 UTC**
(`37 8 * * *`), on manual **Actions > Collect and publish > Run workflow**,
and on changes to application/workflow files on `main`. GitHub schedules can be
delayed; public repositories' schedules may be disabled after 60 days without
repository activity. Check the site's collection timestamp and Actions history.

Each run tests, collects, checks current explanation evidence, builds, commits
the archive and explanation cache, and deploys a Pages artifact
**in the same workflow**. Bot commits do not need to trigger a second workflow.
GitHub Pages must use **GitHub Actions** as its build source.

The workflow has a single non-cancelling concurrency group; each build checks out
the latest `main` after entering that group. A concurrent human push causes the
archive push to fail safely rather than overwrite changes; rerun the workflow.
Only the build job has `contents: write`; only deployment has `pages: write`
and `id-token: write`. Actions are pinned to commit SHAs. Runner-provided Python
and Node are used; application code has no third-party dependencies.

## Sources and selection

| Source | Public endpoint | Selection |
| --- | --- | --- |
| GitHub Status | https://www.githubstatus.com/api/v2/incidents.json | Provider-rated `major` or `critical` incidents |
| Cloudflare Status | https://www.cloudflarestatus.com/api/v2/incidents.json | Provider-rated `major` or `critical` incidents |
| BleepingComputer | https://www.bleepingcomputer.com/feed/ | Reported event plus significant-impact keywords |
| The Hacker News | https://feeds.feedburner.com/TheHackersNews | Reported event plus significant-impact keywords |

This is a **selected feed, not guaranteed coverage or a ranking of the world's
largest incidents**. The first run actually fetches these feeds; no fabricated
live examples are used. Intake rules are deliberately bounded:

- Examine at most 100 entries and accept at most 20 **new** reports per source
  per run, published within 30 days (provider incidents use their start date).
  Older tracked entries still update if present in the inspected feed window.
- Security headlines must indicate an actual reported event (for example
  breach, theft, hijacking, ransomware attack, unauthorized access, or active
  exploitation). Outage headlines must indicate an outage/disruption/failure.
  The headline or teaser must also indicate significant potential impact:
  e.g. global/widespread, thousands/millions, supply chain, hospitals, government,
  payment information, customer data, or cloud-environment breach.
- Exclude common simulation/proof-of-concept, denial, and arrest/sentencing
  reports. Ordinary vulnerability/patch announcements and minor provider
  incidents are not selected. This conservative keyword heuristic still has
  false positives and false negatives; selected reporting is **not a confirmed
  severity rating**. See the explicit expressions in `scripts/pipeline.py`.
- Fetch fixed HTTPS endpoints only, with a 30-second socket timeout, a hard
  40-second per-source subprocess deadline, and a 4 MB response cap.
  Parse RSS, Atom, and Statuspage JSON with the Python
  standard library. The feed collector does not crawl sites. A separate bounded
  explanation check fetches only the explicitly reviewed, allowlisted article
  URLs described below, not arbitrary links found in feeds or articles.

Provider IDs and canonical article URLs deduplicate records across runs.
Known tracking parameters and fragments are removed from URLs. Updated content
replaces the corresponding record; up to ten earlier snapshots are retained
in the JSON archive. Different outlets reporting the same event remain separate
attributed reports rather than being merged speculatively. Counts are reports,
not necessarily distinct real-world incidents.

## Evidence and learning

**Facts and general lessons are separate.** Curated case studies contain original
explanations based on the linked postmortems, with exact dates only where
supported. LastPass's linked retrospective covers multiple 2022 intrusions; its
publication date is not presented as the incident date.

For collected reports, source titles, publication/update timestamps, and short
attributed extracts are shown. Statuspage evidence includes the first and up to
three latest updates (360 characters each). News teasers are limited to
240 characters. Consult the original source for full context and later findings.
Reported claims are attributed, not independently verified.

**Current reports are not restricted to headline summaries.**
`data/current-explanations.json` contains originally written explanations for
five current news reports: what happened, impact, known cause or uncertainty,
response, incident-specific engineering reasoning, and reflection questions.
Each factual section cites short exact passages checked against the source
article. These explanations are visibly labeled source-reviewed, not AI output.
The source's claims remain attributed rather than independently verified.

The initial current explainers group the Optimizely, Barracuda, Fandango, and
FaceStealer reports under security attack, and the Microsoft 365 cascading
failure under capacity/resources. The text explains contributing factors and
remaining unknowns: a single cause filter is not a full causal tree. Even where
an attack mechanism is established, an initial access vector can remain unknown.
Daily collection does **not** erase this reviewed attribution.

All provider reports receive an **automated provider-timeline explanation**:
the named operation, retained reporting checkpoints, actual impact/recovery
notices, and operation-specific engineering lessons (for example merge
reconciliation versus build backlog recovery). Reporting timestamps are not
silently treated as exact outage durations. A resolved provider status is not a
root-cause analysis; these causal groups remain unknown without reviewed evidence.
Timeline reconstructions are labeled separately from the deeper reviewed news
explanations and historical studies.

`python -m scripts.enrichment` checks at most eight reviewed articles per run,
with a 2 MB response cap and a hard 30-second download deadline for each.
Only HTTPS, default-port article paths on `www.bleepingcomputer.com/news/...`
and `thehackernews.com/YYYY/MM/...html` are accepted. Credentials, query strings,
encoded paths, non-article paths, and cross-host redirects are rejected.
No article links are followed. Article text is processed transiently; only
original explanations, hashes, and at most **80 unique quoted words per report**
are retained. Full copyrighted articles are not archived.

The deterministic cache in `data/explanations.json` records the reviewed-text
version, source hash, feed fingerprint, citation checks, and last-check time.
The build is offline and overlays only matching checked explanations on the raw
feed archive. A changed article requires re-review rather than silently
reusing a causal conclusion; a changed feed fingerprint also invalidates the old
overlay. Partial evidence failures and exhausted budgets appear in the report
and explanation-health summary; they do not erase the feed archive.
New news reports without an authored explanation explicitly remain feed-only
until reviewed. **Detailed prose for arbitrary new articles is not generated
automatically.** Current coverage and limitations are displayed on the site.

GitHub Models was tested using the actual Actions `GITHUB_TOKEN` with
`models: read`. The current inference endpoint returned HTTP 200 with plain
`OK`, not a completion response; the legacy endpoint failed DNS resolution.
No working free model access was established, so this release uses the useful
source-reviewed/deterministic alternative rather than fake AI summaries.
The final workflow does not request model permission, call a model, configure
billing, or require a user secret.

Provider start time, source publication/update time, first collection, and last
seen time are distinct fields. News incident dates stay unknown when the feed
does not establish them. A newly published report can describe an old event.
Filters use the incident date when known, otherwise the labeled publication date.
Historical cases and collected reports are visibly differentiated.

## Reliability and safety

`data/incidents.json` is the permanent collected archive;
`data/collection.json` records per-source health and last success;
`data/explanations.json` records explanation coverage, failures, and provenance.
If some sources fail, their previous records and last-success timestamps survive;
the site visibly identifies the failures. If **all** sources fail, collection
exits nonzero before writing either file, the workflow fails, and the previously
published site remains intact. That old site cannot display a failure it has
not received; a browser warning appears when the collection is over 48 hours
old, with a link to workflow history. A reachable, valid but empty feed is healthy,
not a fabricated incident.

Disappeared feed entries remain archived but may no longer receive updates.
Public feeds are finite, can change or become unavailable, and are not an
exhaustive historical database. Earlier snapshots are bounded in each record;
Git history retains committed versions. Long-term archive pruning is manual.
There is no alerting/SLA, and this site is not security advice or a replacement
for official provider communications.

Feed markup is converted to text and HTML-escaped. Only HTTP(S) source links
without embedded credentials are rendered; untrusted text is never inserted
with `innerHTML`. The static page uses a restrictive Content Security Policy,
no analytics, no cookies, no external scripts/fonts, and no server.
The notebook works without JavaScript; filtering requires JavaScript.
Relative asset URLs support the `/incident-learning/` Pages subpath.

## Run locally

Requires Python 3.12+ and Node 20+ (Node is used only for the filter tests).
No installation step is needed.

```powershell
python -m unittest discover -s tests -v
node --test tests/filters.test.mjs
python scripts/pipeline.py collect
python -m scripts.enrichment
python scripts/pipeline.py build
python -m http.server 8000 --directory site
```

Open http://localhost:8000. `site/` is generated and ignored. `build` works offline
using the committed archive. `collect` needs public internet access and updates
the archive/health JSON files. Run enrichment after collection and commit its
cache alongside the feed archive. Skipping enrichment does not make an older
explanation eligible for changed feed evidence.

## Extend the notebook

Add a fixed public endpoint to `SOURCES` in `scripts/pipeline.py` with a unique
ID, display name, URL, and `kind` (`rss` or `statuspage`). Verify its real format,
date fields, selection quality, and access policy; add a focused parser fixture
and document it in the source table. RSS includes Atom support.
Do not add arbitrary user-controlled fetch URLs or full-article storage.

Add an original historical explanation to `data/case-studies.json`, following
the existing fields. Cite a primary account, distinguish incident date from
publication date, explain uncertainty, and separate general lessons from facts.
Supported cause groups: deployment/change, capacity/resources,
dependency/network, configuration, security attack, and unknown. Curating a
live record's source-supported cause is done in `data/current-explanations.json`,
not by editing generated feed records. Read the actual source first, write an
original explanation following an existing entry, cite exact short passages
for each factual section, keep unsupported facts explicitly unknown, and run
the evidence check. The cache and build overlay preserve that attribution
across collection runs. If a checked article changes, review the new account
and update the authored explanation before revalidating it.
For a batch re-review with unchanged wording, update `reviewed_at` only after
checking all included explanations against the current articles.

Contributions should remain concise, source-grounded, and respectful. Never
include leaked personal data, exploit payloads, credentials, or copied articles.
