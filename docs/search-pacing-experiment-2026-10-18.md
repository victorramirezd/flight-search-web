# Production Google Flights pacing comparison — 18 October 2026

Two passes per interval, nine identical date pairs per pass, on production Vercel
compute `iad1`. The comparison began at 10:58:31 UTC. The tested revision was
`c7111978861ee9c9c55f518b6b791933acae6fa8`.

| Minimum request-start gap | Priced queries | Failed queries | Time per nine-pair pass |
| --- | ---: | ---: | --- |
| 1.5 seconds | 16/18 | 2/18 | 14.23s, 13.92s |
| 4 seconds | 18/18 | 0/18 | 33.95s, 33.89s |
| 8 seconds | 12/18 | 6/18 | 65.68s, 64.78s |

All 54 planned queries were attempted. There were no valid-empty results or
unattempted pairs. All eight failures were Google's generic `upstream_error`
payload inside HTTP 200, with `FlightsNotFound`. There were no HTTP 429 responses,
consent failures, parser errors, stream interruptions, or platform timeouts.

## Method

- LIM → MIL, round trip, one adult, economy, EUR, transfers/separate tickets excluded.
- Departures 10, 11 and 12 December 2026; stays of 14, 15 and 16 days: nine pairs.
- Zero to two layovers; up to 20 matching offers per query.
- Pass order: 4s, 1.5s, 8s, then 8s, 1.5s, 4s.
- Fresh consent-aware Google session for each pass; session reused within each pass.
- Thirty-second cooldown between passes. No automatic retries within a pass.
- Existing stop conditions retained; no pass hit the three-consecutive-error stop.
- Diagnostics confirmed approximately 1500ms, 4000ms and 8000ms start gaps. A response
  taking longer than the interval already satisfies the start-gap requirement.

## Interpretation and default

Four seconds performed best in the initial comparison, so it was briefly deployed
as the default in revision `1c26f92644df49deaf7bfdab629638c524a3407b`. A follow-up
production verification immediately returned **two errors in two queries**:

| Departure | Return | Outcome | Measured start gap |
| --- | --- | --- | --- |
| 10 December 2026 | 24 December 2026 | HTTP 200 generic upstream error | First query: no preceding request |
| 10 December 2026 | 25 December 2026 | HTTP 200 generic upstream error | 4000 ms |

These pairs had succeeded in both four-second comparison passes. The follow-up
was outside the balanced comparison and should be reported separately, not treated
as a third identical pass. It demonstrates that the initially clean four-second
result is not reliable enough to justify making every search slower. The original
**1.5-second default is restored** for website/API and CLI. Explicit API intervals
from 1.5 to 15 seconds remain available for controlled testing.

The eight-second runs had more failures than the faster runs, and one failed on its
first query after a cooldown. The evidence does not establish that Google's failures
are caused by request frequency, nor does it exclude rate limiting that is not
reported as HTTP 429. Google load, cache warming, time effects, and other production
traffic were not controlled. Repeating nine unique pairs also limits generalization
to other routes and larger grids.

Reproduce with `scripts/compare_search_pacing.py`. Full per-query diagnostic events
and the password-free manifest are saved locally under ignored
`work/diagnostics/pacing-comparison/`.
