# Flight Search Tool (Python)

This repository contains a Python automation script that searches flights with **flexible dates** and (for round-trips) **flexible trip duration** using the **Amadeus Flight Offers API**.

## Features

- Route-based search (example: `MIL` → `LIM`)
- Flexible departure window around a target date (e.g., ±30 days)
- Round-trip duration range (e.g., 5 to 21 days) or one-way mode
- Best price summary for each departure date
- CSV output and console summary
- API authentication (OAuth2 client credentials)
- Error handling for:
  - Invalid input arguments
  - API rate limits (`429`) with retries
  - Temporary server errors (`5xx`) with retries

## Requirements

- Python 3.10+
- `requests`

Install dependencies:

```bash
pip install requests
```

## API Credentials

Set Amadeus credentials as environment variables:

```bash
export AMADEUS_CLIENT_ID="your_client_id"
export AMADEUS_CLIENT_SECRET="your_client_secret"
```

Or pass them explicitly via CLI flags:

- `--amadeus-client-id`
- `--amadeus-client-secret`

## Usage

### Round-trip flexible search

```bash
python flight_search.py \
  --origin MIL \
  --destination LIM \
  --target-date 2026-07-15 \
  --date-flex-days 30 \
  --min-duration 7 \
  --max-duration 18 \
  --adults 1 \
  --currency EUR \
  --max-results-per-query 20 \
  --output-csv mil_lim_summary.csv
```

### One-way flexible search

```bash
python flight_search.py \
  --origin MIL \
  --destination LIM \
  --target-date 2026-07-15 \
  --date-flex-days 30 \
  --one-way \
  --output-csv mil_lim_oneway.csv
```

## Output

The script prints a summary by departure date and optionally writes CSV output.

Round-trip CSV columns:

- `origin`
- `destination`
- `departure_date`
- `best_return_date`
- `best_price`
- `best_offer_id`

One-way CSV columns:

- `origin`
- `destination`
- `departure_date`
- `best_price`
- `best_offer_id`

## Notes

- The script uses the Amadeus **test** environment URLs by default.
- City codes like `MIL` may work depending on API support; airport-specific codes (e.g., `MXP`) can be used if needed.
- If you hit rate limits, the script retries according to `Retry-After` when provided.
