# Browser request encoding experiment — 18 October 2026

The previous pacing experiment did not establish that slower requests prevent
Google's generic HTTP-200 query failures. This experiment changes query encoding
only: the session, consent handling, parser, date grid, filters, 1.5-second minimum
start interval and three-consecutive-error stop condition remain unchanged.

## Browser evidence and implementation

Matching round-trip LIM→MIL and LIM→MXP searches were built and submitted through
Google Flights' own browser UI for 12–28 December 2026: one adult, economy, EUR,
two stops or fewer, separate/self-transfer tickets hidden. Their exact `tfs` values
are checked in `tests/test_google_query.py`.

The new isolated protobuf schema adds the observed browser envelope (fields 1,
2, 14 and nested 16), endpoint entity types, unpacked passenger enums, URL-safe
unpadded base64, and the captured `tfu` value. MIL uses Google's city entity
`/m/0947l` with type 3; airports retain their IATA codes with type 1. The mapping
contains eight verified metropolitan IDs from Google's returned metadata.
Unmapped cities continue to use their native IATA code; their entity IDs have
not been verified. No lookup request is added to each date query.

The pinned fast-flights package still builds filters and parses results. Its
schema is not mutated globally. Query construction and generated flight links
use the same serialization. `query_encoding=legacy` on the search endpoint
retains the old encoder for controlled comparisons; the default is `browser`.

## Local validation

- 69 Python tests and all three JavaScript suites passed.
- Both city and airport requests match the captured native browser bytes exactly.
- Unit checks cover passenger counts, nonstop filters, transfer filtering in both
  states, baggage, price limits, currency/language, one-way trips, original-query
  preservation and endpoint validation.
- Four live local LIM→MIL/MXP requests for 12–28 December returned priced offers:
  city excluded/included: 9/9; airport excluded/included: 9/10. Each used a fresh
  consent-aware session. These observations are not a claim of future availability.

## Production comparison

The bounded runner supports encoding comparisons with fixed pacing:

```sh
FLIGHT_SEARCH_PASSWORD='<password>' .venv-google/bin/python scripts/compare_search_pacing.py \
  --intervals 1.5,1.5,1.5,1.5 --encodings browser,legacy,legacy,browser \
  --cooldown-seconds 30 --output-dir work/diagnostics/encoding-comparison
```

Each pass has nine date pairs: departures 10–12 December with stays of 14–16 days.
Requests originate from the production backend, use a fresh session per pass and
reuse that session within the pass. A consent exchange can add HTTP requests;
nine date queries are not necessarily nine total HTTP requests. There are no
automatic retries. Results and unattempted pairs are counted separately.

### Observed production results

Deployment: `0c7c31bcb799fd63155aa7a72aaafaec6777b6ac`, region `iad1`.

| Pass | Encoding | Planned | Attempted | Priced | Failed | Unattempted |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | browser | 9 | 9 | 9 | 0 | 0 |
| 2 | legacy | 9 | 9 | 9 | 0 | 0 |
| 3 | legacy | 9 | 9 | 4 | 5 | 0 |
| 4 | browser | 9 | 6 | 2 | 4 | 3 |

All attempted failures were HTTP 200 responses with Google’s generic error
payload. The final browser pass stopped after three consecutive query errors;
its three unattempted pairs do not count as completed or as no availability.

Totals: browser 11 priced / 15 attempted, 4 errors, 3 unattempted; legacy
13 priced / 18 attempted, 5 errors. Both encodings had one clean pass and one
failing pass. This small comparison does **not** establish an improvement in
production reliability. Browser-compatible serialization remains the default
because it matches observed native requests, but it is not a demonstrated fix
for the provider failures. No pacing increase or automatic retries were added.

Request IDs for correlation:
- Pass 1: `69c47157-fe6a-4fd3-a2cd-f9de70ff5f51`
- Pass 2: `9a6b0386-ca67-4b17-8588-a16151cae0db`
- Pass 3: `e2b97fe6-b1e3-4a4f-960a-331586e560cd`
- Pass 4: `2dd836e2-c5c8-4900-990d-952272d511be`

Raw NDJSON and the machine-readable manifest remain in the ignored local
`work/diagnostics/encoding-comparison/` directory. Saved observations are not
current fare availability.
