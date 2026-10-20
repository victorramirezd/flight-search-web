# Flight Search Tool — Google Flights

The `main` branch uses **Google Flights via fast-flights 3.1.0** for both the website
and CLI. No Duffel token is needed for searches.

The [`duffel-version`](https://github.com/victorramirezd/flight-search-web/tree/duffel-version)
branch preserves the Duffel implementation with the updated UI, progress display,
manual retries, price calendar, and Excel copying. It still requires `DUFFEL_ACCESS_TOKEN`.
The consent-aware fetcher rejects optional cookies using Google's returned form and
reuses the same HTTP session, following the local `flight-search` skill.

## Local setup

Use Python 3.12+ and Git:

```sh
python3.12 -m venv .venv-google
.venv-google/bin/python -m pip install -r requirements.txt
.venv-google/bin/python app.py
```

Open <http://127.0.0.1:8000>. The existing web password is `victor` (case-insensitive).
Use `--port 8001` with `app.py` if needed. This local server binds to localhost.

The dependency files pin fast-flights to upstream commit
[`7c105db`](https://github.com/AWeirdDev/flights/commit/7c105db4ed5f73fbadbf6b0e9f6bdfd71f352d97),
the version installed for the skill, and primp to the tested Chrome-145-capable version.
`typing_extensions` is included explicitly because upstream omits it from its dependencies.

## Request encoding

Searches and generated links use a browser-compatible protobuf envelope, tested
against matching Google Flights UI-generated city and airport URLs captured on
18 October 2026. Verified metropolitan entity IDs are stored in
`data/google-city-entities.json`; unmapped cities retain their native IATA code.
The pinned library remains responsible for filters and result parsing.

For controlled diagnostics, `POST /api/search` accepts `query_encoding`:
`browser` (default) or `legacy` (the previous library encoding). This setting
appears in diagnostic exports. It does not change pacing, sessions or retries.
Matching browser serialization does not guarantee Google will return results.

## Try a small search first

Choose **cities or individual airports** (for example, MIL/LON or FCO/LHR), a future departure date,
and set date flex days to `0`. Select one-way for the first test. Prices use EUR and
all displayed departure/arrival times are local to the respective airport.

```sh
.venv-google/bin/python flight_search.py \
  --origin FCO --destination LHR \
  --target-date 2026-11-10 --date-flex-days 0 --one-way \
  --max-connections-per-slice 0 \
  --output-csv work/flights.csv
```

Create the output directory first if exporting CSV. Replace the example date if it
is no longer in the future. The whole flexible departure window must be in the future.

For round-trip comparisons, omit `--one-way` and add, for example,
`--min-duration 7 --max-duration 9`. The tool searches each departure/date-duration
combination sequentially and streams the cheapest matching result per departure date.
Prices cover the configured party of 1–9 adults in economy.

## What this experiment can show

- Flexible departure windows, one-way and round-trip queries, and adult counts
- Best returned priced result per departure date, filtered for outbound layovers
- Airlines, actual airport routes, airport-local segment times, outbound duration
- Google Flights links with the searched route, dates, passengers, EUR and max stops
- Search timestamps, status, and CSV exports including failure messages
- City/airport autocomplete with accent normalization, typo suggestions, and keyboard navigation
- Self-transfer/separate-ticket exclusion enabled by default; switch it off to include them
- Departure and return-window bars on a shared date scale, with hatched overlap
- A readable streaming table, loading spinner and per-query progress slots
- Repeated user-triggered retry passes for remaining failures, preserving earlier fares
- Departure × return price heatmap with per-pair fares, lowest-price highlights and itinerary details
- Copy table as HTML/TSV for pasting into Excel, with plain-text/manual fallbacks

The default excludes self-transfers and separate tickets. To include them in CLI searches,
add `--include-self-transfers`.

## Price calendar and date windows

Both window bars share the earliest departure-to-latest-return date scale. Gold marks
the departure window, green the return window, and hatching marks their actual overlap.
The ruler covers inclusive calendar days and handles windows crossing months/years.

The live calendar has departure dates down the rows and return dates across columns.
Each valid date pair gets its own observed EUR fare, not only the departure's cheapest
summary. Lower prices are green, higher prices warm-colored, and all tied lowest fares
have a “Best” label and outline. Cells distinguish pending, searching, failed, no matching
priced results, and combinations outside the configured trip duration. Selecting a cell
shows its itinerary, observation time and Google Flights link. One-way searches use one
fare column. Earlier successful fares remain visible during recovery.

## Streaming endpoint

`POST /api/search` accepts `exclude_self_transfers` (default `true`) and optional
`retry_queries`: a non-empty list of `{ "departure_date": "YYYY-MM-DD", "return_date": "YYYY-MM-DD" }`
pairs (`null` return date for one-way). Pairs must be unique members of the submitted
search's original date grid. Route, passengers and filters are supplied with every request;
the browser retains the original settings across manual recovery passes.

The NDJSON `start` event includes the ordered `queries` plan, `total` and `is_retry`.
Each query emits `progress` when `running` and when `ok`, `no_offers` or `error`, with
its `query_id`, dates, message, completed count and total. Successful progress events
also contain an `offer` summary for that exact date pair (price, EUR, route, times,
search timestamp and Google Flights URL); other outcomes contain `offer: null`. Existing `row` events carry
the departure summary. Terminal `done`/`error` events include `failed_queries`,
`pending_queries`, `completed` and `total`. A generic `done` event does not establish
availability; failed or unattempted combinations remain explicit. CSV exports include
all per-departure `failed_queries` as JSON, alongside search status and message.

## Limits of this provider

- **Round trips:** the parser returns outbound itinerary details with indicative
  round-trip fares. It does not expose a selected return itinerary. Return dates in
  the table are the dates queried; select and confirm return flights on Google Flights.
  Maximum stops are requested for both directions and checked on outbound results.
  Minimum stops are checked on the outbound only.
- **City codes:** city choices such as MIL/LON use Google's native metropolitan query,
  with one request per date combination. Returned airport endpoints must belong to the
  selected catalog city. This catalog is an identity reference, not a guarantee of service.
- **Self-transfers:** the exclusion setting is passed to Google's native filter and the
  result link. The scraper has no per-result transfer/protection flag, so the app does
  not classify tickets from airline counts. Filtering does not fix provider errors.
- **Flight numbers and booking IDs:** the parser does not provide them. Legacy CSV
  fields `flight_codes` and `best_offer_id` remain `N/A`; no values are invented.
  The legacy `airline_url` field now points to the Google Flights search, rather than
  a specific itinerary or airline booking page.
- **Duration:** outbound duration includes flight time and same-airport connections.
  It is unavailable for airport changes because timezone-safe transfer times are not supplied.
- **Availability:** fares can change. Google can block requests or change its response
  format. A generic Google query error marks that date combination as failed and
  the search moves to the next combination without retrying the failed one. Three
  consecutive query errors stop the search; HTTP/consent/parse failures stop immediately.
  Requests are paced at least 1.5 seconds apart by default. Existing fares, including partial
  results for the current departure, are retained and marked incomplete when stopped.
  An empty result means no matching priced results were returned, not that no flights exist.
  A stream interrupted before its `done` event is shown as incomplete.
  After failures, “Retry failed dates” remains available for additional manual passes
  using a fresh session and the original submitted settings. Waiting between passes
  may help; the UI shows when the last pass finished. Successful queries are not rerun.
  Retries are user-triggered, with one active pass at a time.
  Recovery never erases a cheaper earlier fare. Unattempted queries remain pending;
  use **Continue pending dates** after an early stop to resume only unattempted combinations.
- **Hosting:** local connectivity does not establish that Google's scraper will work
  from a Vercel data-center IP. Large flexible windows can also exceed function time limits.
  Production checks show some city/date pairs returning generic Google errors inside
  HTTP 200 even when identical local queries succeed. Diagnostics preserve that distinction.

## Files

- `app.py`: Vercel entrypoint and local HTTP server
- `api/search.py`: password-protected NDJSON streaming endpoint
- `flight_search.py`: configuration, summary formatting, flexible-date iteration and CLI
- `google_flights.py`: consent handling, query construction and result adaptation
- `index.html`, `airports.js`, `search-ui.js`: browser UI, autocomplete, ledger, streaming and clipboard helpers
- `requirements.txt`, `pyproject.toml`: deployment/runtime dependencies

The airport catalog and its existing Duffel reference audit remain in the repository.
That historical identity audit does **not** establish Google Flights coverage. See
[the catalog validation notes](docs/airport-catalog-validation.md).
`scripts/validate_duffel_catalog.py` remains a separate reference-maintenance utility
that needs a Duffel token; normal flight searches do not call it.

## Verification

```sh
.venv-google/bin/python -m unittest discover -s tests
node tests/airports.test.cjs
node tests/stream.test.cjs
node tests/search-ui.test.cjs
.venv-google/bin/python -m pip check
```

Tests cover consent rejection/session reuse, one-way and round-trip queries, adult
counts, EUR, stop filtering before result limits, local times and duration, missing
fares, blocked-search termination, CSV output, request validation, streaming errors,
city endpoint membership, transfer-filter serialization, targeted retries, progress
counts, repeated recovery passes, shared-scale/overlap geometry, per-pair calendar
prices and colors, HTML/TSV clipboard output, and the existing airport catalog.
Tests use fake provider responses; they do not
establish current flight availability. Run a small real search separately to check
Google connectivity.

## Production search diagnostics and continuation

**Retry failed dates** retries errors only. **Continue pending dates** searches only
unattempted combinations after an early stop; successful fares and failures remain
in the original ledger. Both actions use the original submitted settings. Each pass
uses a fresh consent-aware session, existing pacing, and the same stop conditions.
The API accepts the targeted pending subset through the existing validated
`retry_queries` field; it validates the grid rather than trusting client status.

The expandable **Search diagnostics** panel and failed calendar cells show the
provider error category, Google HTTP status, response kind, and elapsed time when
available. Completed progress events include a native Google Flights `search_url`
even when a query fails, so dates can be checked manually. A generic Google error
inside HTTP 200 does not establish no availability, self-transfer filtering, or IP
blocking as its cause. HTTP 429, HTTP errors, consent errors, missing results data,
network errors, parser errors, and interrupted streams have separate categories.

Each API pass has a request ID, server region, and deployment revision. Structured
server logs and **Copy diagnostics** can be correlated using that ID. The report
includes date-pair outcomes and recent attempts, including recovered errors. It
excludes the search password, cookies, consent tokens, HTTP headers, and raw HTML.

## Comparing production request pacing

`POST /api/search` accepts optional `request_interval_seconds` between 1.5 and 15.
The default remains 1.5 seconds: the [production comparison](docs/search-pacing-experiment-2026-10-18.md)
did not demonstrate a reliable improvement from longer waits.
This is the minimum gap between starts of Google date-pair requests, not an extra
pause after a response. The first query starts immediately. The selected interval
and observed start gap are recorded in diagnostics; filters, session reuse, retry
policy and stop conditions remain the same.

To repeat the bounded LIM→MIL comparison after deploying this option, set
`FLIGHT_SEARCH_PASSWORD` in your environment, then run:

```sh
.venv-google/bin/python scripts/compare_search_pacing.py
```

The script compares nine date pairs at 4, 1.5 and 8 seconds, then reverses that order,
using a fresh session per pass and a 30-second cooldown between passes. It makes no
automatic date-pair retries. A pass may end early under the existing stop conditions;
its unattempted queries are recorded separately rather than counted as failures.
HTTP blocking or stream interruptions stop the experiment. Raw events and a summary
without the password are written under ignored `work/diagnostics/pacing-comparison`.
The small live sample can show an operational difference; it does not establish
Google's internal reason for any failure.


## Comparing Google session reuse

`POST /api/search` accepts optional `session_mode`: `shared` (default, one Google
HTTP client/cookie jar per API pass) or `fresh` (a new client/cookie jar per date
pair). Fresh mode preserves the pass's pacing and consecutive-error stop count.
This is an experiment setting; the public form and CLI retain the shared default.

Completed query diagnostics include `session_mode`, `session_index`,
`http_requests`, `flight_get_requests`, `consent_post_requests` and
`consent_refetch_requests`. Terminal events sum the four request counters under
`request_counts`. These count explicit HTTP client call attempts, including
failed calls; they do not count redirects followed internally by the HTTP
library. A handled consent form normally adds one POST and one extra flight GET.
The refetch counter is a subset of flight GETs: do not add both to the total.
Cookies, consent form values and tokens are never included in these diagnostics.

For a bounded four-pass comparison at fixed pacing and region:

```sh
.venv-google/bin/python scripts/compare_search_pacing.py \
  --intervals 1.5,1.5,1.5,1.5 --session-modes shared,fresh,fresh,shared \
  --expected-region iad1 --cooldown-seconds 30 \
  --output-dir work/diagnostics/session-comparison
```

Set `FLIGHT_SEARCH_PASSWORD` in the environment first. Pass `--expected-revision`
with the full deployment commit to guard against a version change during the
experiment. Each pass starts with a new session; within a pass only fresh mode
replaces the Google session between date pairs. The runner records per-query
outcomes, pending pairs, session counts and consent overhead without automatic
retries. It stops on HTTP blocking, consent failures or interrupted streams.

The [18 October session comparison](docs/search-session-experiment-2026-10-18.md)
did not demonstrate a reliability improvement from fresh sessions; shared remains
the default. US passes required no consent requests; local fresh sessions incurred
additional consent submissions and refetches.

## Five-order deterioration experiment

`POST /api/search` optionally accepts
`experiment: {"departure_order": ["YYYY-MM-DD", ...], "continue_generic_errors": true}`.
Both fields are required when the object is present. The departure list must be
an exact permutation of the selected plan's departures; return dates remain
chronological within each departure. Experiment requests are capped at 25 pairs.
Only recognized Google generic errors in HTTP 200 may continue beyond the normal
three-error stop. Blocking, transport, consent and parser failures still stop.
Ordinary searches keep their existing order and error limit.

The fixed protocol uses 25 LIM→MIL date pairs (10–14 December 2026, 14–18-day
stays), five cyclic departure-group orders, shared sessions per ordering,
1.5-second pacing and 300-second cooldowns. It does not use browser fallback.
Set `FLIGHT_SEARCH_PASSWORD` in the environment and provide the deployed commit:

```sh
.venv-google/bin/python -m pip install matplotlib  # Local report tooling only
.venv-google/bin/python scripts/compare_search_pacing.py --five-order \
  --expected-revision '<full deployed commit>' --expected-region iad1 \
  --output-dir work/diagnostics/five-order-comparison
```

The runner verifies advertised and actual query order, deployment metadata,
settings, session identity and HTTP counters. It saves events after each query,
prints cooldown updates every 30 seconds, and preserves partial results on
cancellation or interruption. Existing run directories cannot be overwritten.
`report.md`, `analysis.json`, PNG/SVG charts and raw NDJSON are generated locally;
passwords and Google session cookies are excluded. Completed queries have actual
UTC start/finish timestamps. Pending or interrupted outcomes never count as
completed observations or absent flights. No automatic resume or retry occurs.
