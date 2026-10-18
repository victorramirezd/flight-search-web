# Frankfurt hosting experiment — 18 October 2026

## Setup

The isolated preview branch `codex/test-frankfurt-region` is based on production
commit `69663ff0328548f529d9d5974d15812a4c2802f6`. Its only difference is a new
`vercel.json` with `"regions": ["fra1"]`. Application code, dependency pins, query
encoding, consent handling, session reuse, filtering, request pacing and stop
conditions are identical. The production domain continues to use `iad1`.

- Frankfurt commit: `bcf0024fdeb21dd72462bf919b88b14c79340d8f`.
- Preview: https://flight-search-web-ushr-7tesyc1zt-victorramirezds-projects.vercel.app/
- Deployment: https://vercel.com/victorramirezds-projects/flight-search-web-ushr/GGKWbnp3w2Fyo69TAmQGMAkobu1v
- Branch: https://github.com/victorramirezd/flight-search-web/tree/codex/test-frankfurt-region

Execution region is verified by the API's `VERCEL_REGION` metadata displayed in
search diagnostics. The CDN's response region alone would not establish where
the Python function actually runs.

Preview protection remains enabled. Frankfurt searches use the authenticated
Safari UI because the Vercel connector denies access to this project and there
are no authenticated local Vercel CLI credentials. US control passes use the
existing HTTP experiment runner against the public production domain. Both
paths invoke the same `/api/search` handler and scraper with identical Google
query settings. Browser cookies are not forwarded to the scraper's Google
session. This is a preview-versus-production comparison, not proof that geography
alone causes a difference.

## Search settings

LIM→MIL, round trip, one adult, economy, EUR; departures 10–12 December 2026;
14–16-day stays; 0–2 outbound layovers; 20 offers per query; self-transfers and
separate tickets excluded; browser-compatible encoding; minimum request start
interval 1.5 seconds. Each pass has nine planned date pairs.

The UI selects target 11 December ±1 day and durations 14–16. The HTTP runner
submits the same nine pairs as a validated subset of target 16 December ±6 days.
The outer date grid does not reach the Google request: the actual departure and
return dates, query ordering, route, passengers and filters are identical.

Each pass gets a fresh consent-aware Google session and reuses it within the
pass. There are no automatic retries. Three consecutive query errors stop a
pass; unattempted dates remain pending. Run order is Frankfurt, US, US, Frankfurt,
with approximately 21 seconds between the first Frankfurt and US passes, then
30 seconds between US passes and more than 30 seconds before the final Frankfurt
pass. Consent exchanges can add HTTP requests
beyond the count of individual date queries.

## Results

| Run order | Environment | Planned | Attempted | Priced | Errors | Pending |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | Frankfurt preview (`fra1`) | 9 | 9 | 8 | 1 | 0 |
| 2 | US production (`iad1`) | 9 | 6 | 3 | 3 | 3 |
| 3 | US production (`iad1`) | 9 | 7 | 3 | 4 | 2 |
| 4 | Frankfurt preview (`fra1`) | 9 | 9 | 8 | 1 | 0 |

Frankfurt: **16 priced / 18 attempted**, 2 errors. US: **6 priced / 13 attempted**,
7 errors and 5 unattempted. All observed failures were generic Google query
errors delivered with HTTP 200, not evidence of absent flights or proven blocking.
Both US passes stopped at the unchanged three-consecutive-error threshold.

The denominators differ because US searches stopped early. Restricting each
Frankfurt pass to the date pairs attempted by its corresponding US pass yields
12 priced results out of 13 Frankfurt observations, versus 6/13 US observations.
These are time-separated observations, not simultaneous randomized trials.

Frankfurt failed 12→27 December in its first pass and 11→26 December in its second.
Each of these pairs succeeded in the other Frankfurt pass. The hypothesis is
therefore promising, but moving regions has not eliminated intermittent failures.
The small sample, unequal cooldown before the first control, changing Google
responses, and preview/production environment differences limit causal claims.

Request IDs:

- Frankfurt 1: `cebb8540-e510-40cd-a798-75362bed15be` (finished 13:11:27 UTC).
- US 1: `ed1f2622-2091-4a6c-ba55-df85deb06d09`.
- US 2: `f02069d0-12f1-4092-aae5-2776c5b263ce`.
- Frankfurt 2: `6956f708-55b1-47bc-b5fc-c4a6bdac0fff` (finished 13:13:30 UTC).

The authenticated preview form and diagnostics visibly confirmed the route,
date grid, filters, revision, 1.5-second request interval and `fra1` region on both
passes. US NDJSON confirmed `iad1` and the production revision. No application
code was changed for this test, so existing tests were not repeated. The region
configuration was checked as valid JSON and verified by the deployed handler.

US raw NDJSON/manifest are retained locally in ignored
`work/diagnostics/region-comparison-us/`. Frankfurt per-pair observations from the
browser accessibility output are retained in ignored
`work/diagnostics/region-comparison-frankfurt/`. They are observations, not current
fare availability.

## Outcome

Keep the Frankfurt preview for broader testing. Production's region setting and
application behavior remain unchanged. A larger interleaved comparison across
more routes/date grids would help decide whether to move production to Frankfurt.
