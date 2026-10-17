# Flight Search Tool

This repository contains a Python flight search script and a Vercel-ready web UI using the **Duffel Flight Offers API**.

## Features

- Flexible departure window around a target date
- Flexible round-trip duration range or one-way mode
- Best price summary for each departure date
- Airline, airport route, flight code, layover, and total duration details
- Maximum layover filtering, defaulting to 2 layovers per slice
- CSV output from the CLI
- Password-protected web search endpoint

## Local Setup

Create and activate a virtual environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install requests
```

Set your Duffel access token:

```bash
export DUFFEL_ACCESS_TOKEN="..."
```

## CLI Usage

```bash
python3 flight_search.py \
  --origin MIL \
  --destination LIM \
  --target-date 2026-08-10 \
  --date-flex-days 4 \
  --min-duration 13 \
  --max-duration 17
```

## Website

The web app is made of:

- `index.html`: the browser UI
- `api/search.py`: the Vercel serverless search endpoint
- `flight_search.py`: shared Duffel search logic
- `scripts/enrich_airport_coordinates.py`: enriches the airport option list with coordinates from OurAirports
- `requirements.txt`: Python dependencies for Vercel

The web search password is currently hardcoded in `api/search.py` as:

```python
SEARCH_PASSWORD = "victor"
```

The check is case-insensitive, so `Victor` works in the web form.

Set the Duffel token in Vercel:

```bash
vercel env add DUFFEL_ACCESS_TOKEN
```

Deploy preview:

```bash
vercel
```

Deploy production:

```bash
vercel --prod
```

## Output Columns

- `origin`
- `destination`
- `departure_date`
- `best_return_date` for round trips
- `best_price`
- `currency`
- `airlines`
- `airports`
- `flight_codes`
- `flight_segments`
- `layovers`
- `total_duration`
- `mode`
- `best_offer_id`

## Notes

- Duffel test tokens start with `duffel_test_`; test mode can return unrealistic schedules, prices, and flight numbers.
- The script defaults to `--max-connections-per-slice 2`, excluding offers with more than two layovers in any outbound or return slice.
- Use `--min-connections-per-slice 1` to exclude direct-looking offers for routes where direct flights should not exist.
- Keep `.env` and `.venv/` out of Git. They are ignored by `.gitignore`.

## Location search

The web form ranks city, airport-name and IATA matches, with accent normalization and
limited typo suggestions when there are no direct matches. Choose a suggestion using
the mouse or Arrow Up/Down and Enter; Escape closes the list. Selected locations use
compact labels such as `Palermo (PMO)`. Exact known IATA codes and unambiguous names
also resolve on submission. Ambiguous names require a selection.

Only catalog codes are submitted to Duffel. The catalog is not a guarantee of scheduled
service or Duffel coverage. The Search status column distinguishes no matching offers
from failed date searches, and flags incomplete prices when some date searches fail.

Run regression checks with Python 3.12+ and Node.js:

```sh
python -m unittest discover -s tests
node tests/airports.test.cjs
```

Backend tests use a fake provider and do not call Duffel.

## Validate the full catalog against Duffel

`scripts/validate_duffel_catalog.py` downloads every page of Duffel v2's airport
and city reference lists, then compares every local entry by IATA code, location
type and country. It does not create flight offer requests or bookings.

Make `DUFFEL_ACCESS_TOKEN` available in your local environment, then run:

```sh
python scripts/validate_duffel_catalog.py --output work/duffel-catalog
```

Keep the token out of shell history, source control and reports. The script does
not automatically load `.env` files. It needs the existing `requests` dependency.

The output contains a complete reference snapshot, per-entry JSON/CSV comparisons,
a summary with retrieval time and hashes, and a proposed corrected catalog. The
original catalog is unchanged unless you explicitly add `--write-catalog`.
A failed download, repeated cursor or missing pagination metadata stops validation
before it writes a report or changes the catalog.

Confirmed entries use Duffel's airport/city identities and metropolitan membership.
The validator also reads airports embedded in city records, with top-level airport
records preferred over duplicate membership aliases. Codes defined only as cities
are labelled as cities instead of individual airports.

Local coordinates are retained because Duffel's reference geography contains some
large discrepancies. Differences over 100 km are flagged, with the local name and
city retained as well. If local coordinates are missing, valid Duffel coordinates
are used as a fallback. Country conflicts are accepted only when the exact code and
coordinates within 5 km identify the same airport; the local country is retained
and Duffel's differing country is explicitly recorded. Distant country/code
collisions and codes absent from both reference lists are excluded and reported.

Alternate names are retained as search aliases for matching identities, but never
transferred from an old airport label to a city or across a large coordinate
conflict. Review exclusions and metadata warnings before applying a catalog.

Validation confirms reference identity, not scheduled service, flight availability
or bookability. Repeat periodically because provider reference data changes.

The current validation evidence and correction decisions are documented in
`docs/airport-catalog-validation.md`. The per-entry comparison is in
`data/airport-validation.csv`, and its summary/hashes are in
`data/airport-validation.json`. Re-running the older OurAirports coordinate
utility on the validated catalog is disabled; refresh via this validator instead.
