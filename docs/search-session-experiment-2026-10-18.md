# Google session reuse experiment — 18 October 2026

## Controlled change

`POST /api/search` accepts `session_mode=shared` (the unchanged default) or
`session_mode=fresh`. Shared mode reuses one Google HTTP client/cookie jar for a
whole API pass. Fresh mode replaces the entire client/cookie jar before every
subsequent date pair. It does not reset request pacing or the consecutive-error
counter. Both modes begin each API pass with a new Google session.

Both experiment arms run in production's `iad1` region with browser-compatible
encoding. The Frankfurt preview remains independent. Route, date pairs, passenger
count, currency, cabin, stop/transfer filters, consent policy, result parser,
pacing and three-consecutive-error stop condition are identical between arms.
A longer consent exchange may make an actual start gap exceed 1.5 seconds; the
minimum configured gap remains 1.5 seconds in both modes.

Per-query diagnostics record `session_mode`, `session_index`, `http_requests`,
`flight_get_requests`, `consent_post_requests` and `consent_refetch_requests`.
Terminal events sum request counters in `request_counts`. The runner verifies
these totals and can require an exact deployment revision and execution region.

Counters count explicit client call attempts, including failures. HTTP-library
redirects are not counted separately. `flight_get_requests` includes the initial
flight-page GET plus any consent refetch. A normal consent exchange adds one
consent POST and one refetch GET, i.e. two extra explicit requests. Do not add
`consent_refetch_requests` to `flight_get_requests` again. No cookie values,
consent tokens, request headers or raw response bodies are exported.

## Verification

75 Python tests and all three JavaScript suites passed. Checks cover independent
clients/cookie jars, shared reuse, per-query counter resets, successful and failed
consent exchanges, preserved pacing and consecutive-failure limits, invalid API
inputs, default shared mode, targeted retries, terminal counter aggregation on
success/failure, and safe browser diagnostic exports.

A bounded local live check searched LIM→MIL for 11 and 12 December, returning
28 December, with one adult, economy, EUR, at most two stops and self-transfers
excluded. All four queries returned priced offers:

| Mode | Queries | Sessions | Flight GETs | Consent POSTs | Consent refetches | Total explicit HTTP calls |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Shared | 2 | 1 | 3 | 1 | 1 | 4 |
| Fresh | 2 | 2 | 4 | 2 | 2 | 6 |

This verifies consent overhead locally; it does not establish production
reliability. Observed prices/availability can change.

## Production experiment

Nine date pairs per pass: LIM→MIL, departures 10–12 December 2026, with stays of
14–16 days. One adult, economy, EUR, 0–2 outbound layovers, 20 offers per query,
self-transfers and separate tickets excluded, 1.5-second minimum start interval.
The fixed query grid is the same as the preceding region and encoding tests.

Order: shared, fresh, fresh, shared, with a 30-second cooldown between passes.
There are no automatic retries or concurrent Google queries. Generic provider
errors retain unknown availability; unattempted pairs remain pending. The entire
comparison stops on HTTP blocking, consent/missing-results failures or stream
interruption. A generic error pass may stop at three consecutive errors while
later experimental passes still start with fresh sessions after cooldown.

Reproduce with `FLIGHT_SEARCH_PASSWORD` set in the environment:

```sh
.venv-google/bin/python scripts/compare_search_pacing.py \
  --intervals 1.5,1.5,1.5,1.5 --session-modes shared,fresh,fresh,shared \
  --expected-region iad1 --expected-revision '<full deployed commit>' \
  --cooldown-seconds 30 --output-dir work/diagnostics/session-comparison
```

### Observed production results

All four passes confirmed region `iad1` and deployment
`ba91522e6db9e39aa3ab5a95db3c748f3040fb01`.

| Pass | Mode | Planned | Attempted | Priced | Errors | Pending | Sessions | Explicit HTTP calls | Consent POSTs | Consent refetches |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | shared | 9 | 9 | 5 | 4 | 0 | 1 | 9 | 0 | 0 |
| 2 | fresh | 9 | 9 | 6 | 3 | 0 | 9 | 9 | 0 | 0 |
| 3 | fresh | 9 | 5 | 2 | 3 | 4 | 5 | 5 | 0 | 0 |
| 4 | shared | 9 | 9 | 6 | 3 | 0 | 1 | 9 | 0 | 0 |

**Shared totals:** 11 priced / 18 attempted, 7 errors, no pending dates, 2 sessions,
18 explicit HTTP calls. **Fresh totals:** 8 priced / 14 attempted, 6 errors,
4 unattempted, 14 sessions, 14 explicit HTTP calls.

All recorded gaps between query starts were 1,500 ms (rounded) in both modes.
All 13 observed failures were Google generic query errors in HTTP 200 responses.
The second fresh pass stopped after three consecutive errors; its four pending
pairs do not count as completed, failed or absent flights. Per-query counters
matched terminal totals in every pass. No US consent submissions or refetches
occurred in either mode, so observed consent overhead was zero in this region.

Request IDs:
- Pass 1 (shared): `48f1d84e-0e5a-4c3d-8d30-1a321c9b4dd3`.
- Pass 2 (fresh): `728f1b83-8e99-426b-8c41-ded766f63772`.
- Pass 3 (fresh): `5d2b8e71-b5db-4887-8c1b-4e0e9a4e31db`.
- Pass 4 (shared): `91f1f364-7f4d-4b61-8c23-984c684b357a`.

Raw NDJSON and the manifest remain in ignored
`work/diagnostics/session-comparison/`. Local consent observations are saved in
`work/diagnostics/session-local.json`.

## Outcome

Fresh sessions did **not** demonstrate a reliability improvement in this bounded
US-region experiment. Both modes produced intermittent generic Google errors.
The denominators differ because the unchanged stop condition truncated one
fresh pass, and the sample is too small for a causal conclusion. Keep shared
sessions as the default; fresh mode remains available for controlled API tests.

The local EU check showed higher consent overhead with fresh sessions, while
the US comparison required none. Do not extrapolate US request counts to the
Frankfurt preview without measuring consent there. The Frankfurt region
experiment remains unchanged and should be evaluated independently.
