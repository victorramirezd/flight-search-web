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
- `requirements.txt`: Python dependencies for Vercel

The web search password is currently hardcoded in `api/search.py` as:

```python
SEARCH_PASSWORD = "per" + "u"
```

The check is case-insensitive, so `Peru` works in the web form.

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
