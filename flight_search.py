#!/usr/bin/env python3
"""Flight search automation tool using the Amadeus API.

Features:
- Flexible departure date window around a target date (±N days)
- Flexible trip durations (for round-trip searches)
- Best-price summary by departure date
- CSV and/or console output
- API authentication handling
- Basic rate-limit and transient-error retries
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

import requests


AMADUES_AUTH_URL = "https://test.api.amadeus.com/v1/security/oauth2/token"
AMADUES_FLIGHT_OFFERS_URL = "https://test.api.amadeus.com/v2/shopping/flight-offers"


@dataclass(frozen=True)
class SearchConfig:
    origin: str
    destination: str
    target_date: date
    date_flex_days: int
    one_way: bool
    min_duration: Optional[int]
    max_duration: Optional[int]
    adults: int
    currency: str
    max_results_per_query: int
    output_csv: Optional[str]


class ApiError(Exception):
    """Raised for non-recoverable API errors."""


class RateLimitError(ApiError):
    """Raised when the API rate limit has been reached and retries are exhausted."""


class AmadeusClient:
    def __init__(self, client_id: str, client_secret: str, session: Optional[requests.Session] = None) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.session = session or requests.Session()
        self.access_token: Optional[str] = None
        self.token_expiry_epoch = 0.0

    def authenticate(self) -> None:
        payload = {
            "grant_type": "client_credentials",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        }
        response = self.session.post(AMADUES_AUTH_URL, data=payload, timeout=30)

        if response.status_code != 200:
            raise ApiError(
                f"Authentication failed ({response.status_code}): {response.text[:300]}"
            )

        token_data = response.json()
        self.access_token = token_data["access_token"]
        expires_in = int(token_data.get("expires_in", 0))
        self.token_expiry_epoch = time.time() + max(expires_in - 60, 0)

    def _ensure_token(self) -> None:
        if not self.access_token or time.time() >= self.token_expiry_epoch:
            self.authenticate()

    def search_offers(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: Optional[date],
        adults: int,
        currency: str,
        max_results: int,
        retries: int = 3,
    ) -> List[dict]:
        self._ensure_token()
        assert self.access_token, "Access token should be present after _ensure_token"

        params = {
            "originLocationCode": origin,
            "destinationLocationCode": destination,
            "departureDate": departure_date.isoformat(),
            "adults": str(adults),
            "currencyCode": currency,
            "max": str(max_results),
        }
        if return_date:
            params["returnDate"] = return_date.isoformat()

        headers = {"Authorization": f"Bearer {self.access_token}"}

        for attempt in range(retries + 1):
            response = self.session.get(
                AMADUES_FLIGHT_OFFERS_URL,
                params=params,
                headers=headers,
                timeout=45,
            )

            if response.status_code == 200:
                payload = response.json()
                return payload.get("data", [])

            if response.status_code == 401 and attempt < retries:
                # Token may have expired unexpectedly.
                self.authenticate()
                headers["Authorization"] = f"Bearer {self.access_token}"
                continue

            if response.status_code == 429:
                retry_after_header = response.headers.get("Retry-After", "2")
                try:
                    retry_after_seconds = int(retry_after_header)
                except ValueError:
                    retry_after_seconds = 2

                if attempt < retries:
                    sleep_seconds = retry_after_seconds * (attempt + 1)
                    time.sleep(sleep_seconds)
                    continue

                raise RateLimitError(
                    "Rate limit reached and retries exhausted. "
                    f"Last response: {response.text[:300]}"
                )

            if 500 <= response.status_code < 600 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue

            raise ApiError(
                f"Flight search failed ({response.status_code}): {response.text[:300]}"
            )

        raise ApiError("Unexpected retry loop termination")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search for best flights over flexible dates.")
    parser.add_argument("--origin", required=True, help="IATA origin code (e.g., MIL or MXP)")
    parser.add_argument("--destination", required=True, help="IATA destination code (e.g., LIM)")
    parser.add_argument(
        "--target-date",
        required=True,
        help="Target departure date in YYYY-MM-DD format",
    )
    parser.add_argument(
        "--date-flex-days",
        type=int,
        default=30,
        help="Days before/after target departure date to search (default: 30)",
    )
    parser.add_argument(
        "--one-way",
        action="store_true",
        help="Search one-way flights (default behavior is round-trip)",
    )
    parser.add_argument(
        "--min-duration",
        type=int,
        default=5,
        help="Minimum trip duration in days for round-trip searches (default: 5)",
    )
    parser.add_argument(
        "--max-duration",
        type=int,
        default=21,
        help="Maximum trip duration in days for round-trip searches (default: 21)",
    )
    parser.add_argument("--adults", type=int, default=1, help="Number of adult travelers")
    parser.add_argument("--currency", default="EUR", help="Currency code (default: EUR)")
    parser.add_argument(
        "--max-results-per-query",
        type=int,
        default=20,
        help="Max offers returned by API per query (default: 20)",
    )
    parser.add_argument(
        "--output-csv",
        help="Optional path to write CSV summary",
    )
    parser.add_argument(
        "--amadeus-client-id",
        default=os.getenv("AMADEUS_CLIENT_ID"),
        help="Amadeus API key/client id (or set AMADEUS_CLIENT_ID)",
    )
    parser.add_argument(
        "--amadeus-client-secret",
        default=os.getenv("AMADEUS_CLIENT_SECRET"),
        help="Amadeus API secret (or set AMADEUS_CLIENT_SECRET)",
    )

    return parser.parse_args()


def date_range(center: date, flex_days: int) -> Iterable[date]:
    for offset in range(-flex_days, flex_days + 1):
        yield center + timedelta(days=offset)


def best_offer_summary(offers: List[dict]) -> Optional[Tuple[float, str]]:
    best_price: Optional[float] = None
    best_offer_id = ""

    for offer in offers:
        try:
            price = float(offer["price"]["total"])
            offer_id = offer.get("id", "N/A")
        except (KeyError, ValueError, TypeError):
            continue

        if best_price is None or price < best_price:
            best_price = price
            best_offer_id = offer_id

    if best_price is None:
        return None
    return best_price, best_offer_id


def validate_config(args: argparse.Namespace) -> SearchConfig:
    try:
        target = datetime.strptime(args.target_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("--target-date must be in YYYY-MM-DD format") from exc

    if args.date_flex_days < 0:
        raise ValueError("--date-flex-days must be >= 0")
    if args.adults < 1:
        raise ValueError("--adults must be >= 1")
    if args.max_results_per_query < 1:
        raise ValueError("--max-results-per-query must be >= 1")

    min_duration = None if args.one_way else args.min_duration
    max_duration = None if args.one_way else args.max_duration
    if not args.one_way:
        if min_duration is None or max_duration is None:
            raise ValueError("--min-duration and --max-duration are required for round-trip mode")
        if min_duration < 1:
            raise ValueError("--min-duration must be >= 1")
        if max_duration < min_duration:
            raise ValueError("--max-duration must be >= --min-duration")

    return SearchConfig(
        origin=args.origin.upper(),
        destination=args.destination.upper(),
        target_date=target,
        date_flex_days=args.date_flex_days,
        one_way=args.one_way,
        min_duration=min_duration,
        max_duration=max_duration,
        adults=args.adults,
        currency=args.currency.upper(),
        max_results_per_query=args.max_results_per_query,
        output_csv=args.output_csv,
    )


def run_search(client: AmadeusClient, config: SearchConfig) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for dep_date in date_range(config.target_date, config.date_flex_days):
        if config.one_way:
            return_dates = [None]
        else:
            assert config.min_duration is not None and config.max_duration is not None
            return_dates = [
                dep_date + timedelta(days=d) for d in range(config.min_duration, config.max_duration + 1)
            ]

        best_for_departure: Optional[Tuple[float, date, str]] = None

        for ret_date in return_dates:
            try:
                offers = client.search_offers(
                    origin=config.origin,
                    destination=config.destination,
                    departure_date=dep_date,
                    return_date=ret_date,
                    adults=config.adults,
                    currency=config.currency,
                    max_results=config.max_results_per_query,
                )
            except RateLimitError:
                raise
            except ApiError as err:
                print(
                    f"Warning: query failed for departure {dep_date} "
                    f"return {ret_date}: {err}",
                    file=sys.stderr,
                )
                continue

            summary = best_offer_summary(offers)
            if not summary:
                continue

            price, offer_id = summary
            if best_for_departure is None or price < best_for_departure[0]:
                best_for_departure = (price, ret_date if ret_date else dep_date, offer_id)

        row: Dict[str, str] = {
            "origin": config.origin,
            "destination": config.destination,
            "departure_date": dep_date.isoformat(),
        }
        if best_for_departure:
            row["best_price"] = f"{best_for_departure[0]:.2f}"
            row["best_offer_id"] = best_for_departure[2]
            if not config.one_way:
                row["best_return_date"] = best_for_departure[1].isoformat()
        else:
            row["best_price"] = "N/A"
            row["best_offer_id"] = "N/A"
            if not config.one_way:
                row["best_return_date"] = "N/A"

        rows.append(row)

    return rows


def print_summary(rows: List[Dict[str, str]], one_way: bool) -> None:
    print("\nBest flight prices by departure date")
    if one_way:
        print("departure_date | best_price | offer_id")
        for row in rows:
            print(f"{row['departure_date']} | {row['best_price']} | {row['best_offer_id']}")
    else:
        print("departure_date | best_return_date | best_price | offer_id")
        for row in rows:
            print(
                f"{row['departure_date']} | {row['best_return_date']} | "
                f"{row['best_price']} | {row['best_offer_id']}"
            )


def write_csv(rows: List[Dict[str, str]], csv_path: str, one_way: bool) -> None:
    fieldnames = ["origin", "destination", "departure_date"]
    if not one_way:
        fieldnames.append("best_return_date")
    fieldnames.extend(["best_price", "best_offer_id"])

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    args = parse_args()

    if not args.amadeus_client_id or not args.amadeus_client_secret:
        print(
            "Missing API credentials. Provide --amadeus-client-id and "
            "--amadeus-client-secret or set AMADEUS_CLIENT_ID/AMADEUS_CLIENT_SECRET.",
            file=sys.stderr,
        )
        return 2

    try:
        config = validate_config(args)
    except ValueError as err:
        print(f"Invalid arguments: {err}", file=sys.stderr)
        return 2

    client = AmadeusClient(args.amadeus_client_id, args.amadeus_client_secret)

    try:
        rows = run_search(client, config)
    except RateLimitError as err:
        print(
            f"API rate limit reached and retries exhausted. Please retry later. Details: {err}",
            file=sys.stderr,
        )
        return 3
    except ApiError as err:
        print(f"API error: {err}", file=sys.stderr)
        return 4
    except requests.RequestException as err:
        print(f"Network error while contacting API: {err}", file=sys.stderr)
        return 5

    print_summary(rows, one_way=config.one_way)

    if config.output_csv:
        write_csv(rows, config.output_csv, one_way=config.one_way)
        print(f"\nCSV summary written to: {config.output_csv}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
