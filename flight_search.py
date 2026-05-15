#!/usr/bin/env python3
"""Flight search automation tool using the Duffel API."""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional

import requests


DUFFEL_OFFER_REQUESTS_URL = "https://api.duffel.com/air/offer_requests"
DUFFEL_API_VERSION = "v2"


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
    max_results_per_query: int
    min_connections_per_slice: int
    max_connections_per_slice: Optional[int]
    output_csv: Optional[str] = None


@dataclass(frozen=True)
class OfferSummary:
    price: float
    currency: str
    offer_id: str
    airlines: str
    airports: str
    flight_codes: str
    flight_segments: str
    layovers: str
    total_duration: str
    mode: str


class ApiError(Exception):
    """Raised for non-recoverable API errors."""


class RateLimitError(ApiError):
    """Raised when the API rate limit has been reached and retries are exhausted."""


def api_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]

    errors = payload.get("errors")
    if not isinstance(errors, list):
        return response.text[:300]

    messages = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        title = error.get("title")
        message = error.get("message")
        source = error.get("source")
        pointer = source.get("pointer") if isinstance(source, dict) else None

        details = []
        if pointer:
            details.append(str(pointer))
        if title and message:
            details.append(f"{title}: {message}")
        elif message:
            details.append(str(message))
        elif title:
            details.append(str(title))

        if details:
            messages.append(" - ".join(details))

    return "; ".join(messages) if messages else response.text[:300]


def cheapest_offers(offers: List[dict], limit: int) -> List[dict]:
    def price_or_infinity(offer: dict) -> float:
        try:
            return float(offer["total_amount"])
        except (KeyError, ValueError, TypeError):
            return float("inf")

    return sorted(offers, key=price_or_infinity)[:limit]


def offer_mode(offer: dict) -> str:
    live_mode = offer.get("live_mode", offer.get("_offer_request_live_mode"))
    if live_mode is True:
        return "live"
    if live_mode is False:
        return "test"
    return "unknown"


def connections_in_slice(flight_slice: dict) -> int:
    segments = flight_slice.get("segments", [])
    if not isinstance(segments, list):
        return 0
    return max(len([segment for segment in segments if isinstance(segment, dict)]) - 1, 0)


def offer_meets_min_connections(offer: dict, min_connections_per_slice: int) -> bool:
    if min_connections_per_slice <= 0:
        return True

    slices = offer.get("slices", [])
    if not isinstance(slices, list) or not slices:
        return False

    for flight_slice in slices:
        if not isinstance(flight_slice, dict):
            return False
        if connections_in_slice(flight_slice) < min_connections_per_slice:
            return False

    return True


def offer_meets_max_connections(offer: dict, max_connections_per_slice: Optional[int]) -> bool:
    if max_connections_per_slice is None:
        return True

    slices = offer.get("slices", [])
    if not isinstance(slices, list) or not slices:
        return False

    for flight_slice in slices:
        if not isinstance(flight_slice, dict):
            return False
        if connections_in_slice(flight_slice) > max_connections_per_slice:
            return False

    return True


def parse_iso_duration_minutes(duration: str) -> Optional[int]:
    match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?T?(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?",
        duration,
    )
    if not match:
        return None

    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    return days * 24 * 60 + hours * 60 + minutes


def format_duration(minutes: Optional[int]) -> str:
    if minutes is None:
        return "N/A"

    hours, remaining_minutes = divmod(minutes, 60)
    if hours and remaining_minutes:
        return f"{hours}h {remaining_minutes}m"
    if hours:
        return f"{hours}h"
    return f"{remaining_minutes}m"


def airport_label(airport: dict) -> str:
    return airport.get("iata_code") or airport.get("name") or "unknown"


def offer_segments(offer: dict) -> Iterable[dict]:
    for flight_slice in offer.get("slices", []):
        if not isinstance(flight_slice, dict):
            continue
        for segment in flight_slice.get("segments", []):
            if isinstance(segment, dict):
                yield segment


def offer_airlines(offer: dict) -> str:
    airlines: List[str] = []
    seen = set()

    for segment in offer_segments(offer):
        carrier = segment.get("operating_carrier")
        if not isinstance(carrier, dict):
            continue
        airline = carrier.get("name") or carrier.get("iata_code")
        if airline and airline not in seen:
            seen.add(airline)
            airlines.append(airline)

    return ", ".join(airlines) if airlines else "N/A"


def offer_airports(offer: dict) -> str:
    routes: List[str] = []

    for flight_slice in offer.get("slices", []):
        if not isinstance(flight_slice, dict):
            continue
        segments = [
            segment for segment in flight_slice.get("segments", []) if isinstance(segment, dict)
        ]
        if not segments:
            continue

        route: List[str] = []
        first_origin = segments[0].get("origin")
        if isinstance(first_origin, dict):
            route.append(airport_label(first_origin))

        for segment in segments:
            destination = segment.get("destination")
            if isinstance(destination, dict):
                route.append(airport_label(destination))

        if route:
            routes.append(" -> ".join(route))

    return " / ".join(routes) if routes else "N/A"


def carrier_code(carrier: object) -> Optional[str]:
    if not isinstance(carrier, dict):
        return None
    return carrier.get("iata_code")


def flight_code(carrier: object, flight_number: object) -> Optional[str]:
    if not flight_number:
        return None

    code = carrier_code(carrier)
    if code:
        return f"{code}{flight_number}"

    return str(flight_number)


def segment_flight_code(segment: dict) -> Optional[str]:
    marketing_code = flight_code(
        segment.get("marketing_carrier"),
        segment.get("marketing_carrier_flight_number"),
    )
    operating_code = flight_code(
        segment.get("operating_carrier"),
        segment.get("operating_carrier_flight_number"),
    )

    if marketing_code and operating_code and marketing_code != operating_code:
        return f"{marketing_code} operated as {operating_code}"
    if marketing_code:
        return marketing_code
    if operating_code:
        return operating_code

    return None


def offer_flight_codes(offer: dict) -> str:
    flight_codes = []

    for segment in offer_segments(offer):
        flight_code = segment_flight_code(segment)
        if flight_code:
            flight_codes.append(flight_code)

    return ", ".join(flight_codes) if flight_codes else "N/A"


def segment_route(segment: dict) -> str:
    origin = segment.get("origin")
    destination = segment.get("destination")
    origin_label = airport_label(origin) if isinstance(origin, dict) else "unknown"
    destination_label = airport_label(destination) if isinstance(destination, dict) else "unknown"
    return f"{origin_label}->{destination_label}"


def offer_flight_segments(offer: dict) -> str:
    segment_details = []

    for segment in offer_segments(offer):
        flight_code = segment_flight_code(segment) or "N/A"
        segment_details.append(f"{segment_route(segment)}: {flight_code}")

    return "; ".join(segment_details) if segment_details else "N/A"


def offer_layovers(offer: dict) -> str:
    layover_airports: List[str] = []

    for flight_slice in offer.get("slices", []):
        if not isinstance(flight_slice, dict):
            continue
        segments = [
            segment for segment in flight_slice.get("segments", []) if isinstance(segment, dict)
        ]
        for segment in segments[:-1]:
            destination = segment.get("destination")
            if isinstance(destination, dict):
                layover_airports.append(airport_label(destination))

    if not layover_airports:
        return "0"
    return f"{len(layover_airports)} ({', '.join(layover_airports)})"


def offer_total_duration(offer: dict) -> str:
    total_minutes = 0
    found_duration = False

    for flight_slice in offer.get("slices", []):
        if not isinstance(flight_slice, dict):
            continue
        duration = flight_slice.get("duration")
        if not isinstance(duration, str):
            continue
        minutes = parse_iso_duration_minutes(duration)
        if minutes is None:
            continue
        total_minutes += minutes
        found_duration = True

    if found_duration:
        return format_duration(total_minutes)

    for segment in offer_segments(offer):
        duration = segment.get("duration")
        if not isinstance(duration, str):
            continue
        minutes = parse_iso_duration_minutes(duration)
        if minutes is None:
            continue
        total_minutes += minutes
        found_duration = True

    return format_duration(total_minutes if found_duration else None)


class DuffelClient:
    def __init__(self, access_token: str, session: Optional[requests.Session] = None) -> None:
        self.access_token = access_token
        self.session = session or requests.Session()

    def search_offers(
        self,
        *,
        origin: str,
        destination: str,
        departure_date: date,
        return_date: Optional[date],
        adults: int,
        max_results: int,
        retries: int = 3,
    ) -> List[dict]:
        slices = [
            {
                "origin": origin,
                "destination": destination,
                "departure_date": departure_date.isoformat(),
            }
        ]
        if return_date:
            slices.append(
                {
                    "origin": destination,
                    "destination": origin,
                    "departure_date": return_date.isoformat(),
                }
            )

        payload = {
            "data": {
                "slices": slices,
                "passengers": [{"type": "adult"} for _ in range(adults)],
                "cabin_class": "economy",
            }
        }

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Duffel-Version": DUFFEL_API_VERSION,
            "Authorization": f"Bearer {self.access_token}",
        }

        for attempt in range(retries + 1):
            response = self.session.post(
                DUFFEL_OFFER_REQUESTS_URL,
                params={"return_offers": "true"},
                json=payload,
                headers=headers,
                timeout=45,
            )

            if response.status_code in (200, 201):
                response_payload = response.json()
                data = response_payload.get("data", {})
                offers = data.get("offers", [])
                for offer in offers:
                    if isinstance(offer, dict):
                        offer["_offer_request_live_mode"] = data.get("live_mode")
                        offer["_offer_request_id"] = data.get("id")
                return cheapest_offers(offers, max_results)

            if response.status_code == 429:
                retry_after_header = response.headers.get("Retry-After") or response.headers.get(
                    "ratelimit-reset", "2"
                )
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
                    f"Last response: {api_error_message(response)}"
                )

            if 500 <= response.status_code < 600 and attempt < retries:
                time.sleep(2 * (attempt + 1))
                continue

            raise ApiError(
                f"Flight search failed ({response.status_code}): {api_error_message(response)}"
            )

        raise ApiError("Unexpected retry loop termination")


def date_range(center: date, flex_days: int) -> Iterable[date]:
    for offset in range(-flex_days, flex_days + 1):
        yield center + timedelta(days=offset)


def best_offer_summary(
    offers: List[dict],
    min_connections_per_slice: int = 0,
    max_connections_per_slice: Optional[int] = 2,
) -> Optional[OfferSummary]:
    best_summary: Optional[OfferSummary] = None

    for offer in offers:
        if not offer_meets_min_connections(offer, min_connections_per_slice):
            continue
        if not offer_meets_max_connections(offer, max_connections_per_slice):
            continue

        try:
            price = float(offer["total_amount"])
            offer_id = offer.get("id", "N/A")
            currency = offer.get("total_currency", "")
        except (KeyError, ValueError, TypeError):
            continue

        if best_summary is None or price < best_summary.price:
            best_summary = OfferSummary(
                price=price,
                currency=currency,
                offer_id=offer_id,
                airlines=offer_airlines(offer),
                airports=offer_airports(offer),
                flight_codes=offer_flight_codes(offer),
                flight_segments=offer_flight_segments(offer),
                layovers=offer_layovers(offer),
                total_duration=offer_total_duration(offer),
                mode=offer_mode(offer),
            )

    return best_summary


def build_search_config(
    *,
    origin: str,
    destination: str,
    target_date: str,
    date_flex_days: int = 30,
    one_way: bool = False,
    min_duration: int = 5,
    max_duration: int = 21,
    adults: int = 1,
    max_results_per_query: int = 20,
    min_connections_per_slice: int = 0,
    max_connections_per_slice: Optional[int] = 2,
    output_csv: Optional[str] = None,
) -> SearchConfig:
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("target_date must be in YYYY-MM-DD format") from exc

    if date_flex_days < 0:
        raise ValueError("date_flex_days must be >= 0")
    if adults < 1:
        raise ValueError("adults must be >= 1")
    if max_results_per_query < 1:
        raise ValueError("max_results_per_query must be >= 1")
    if min_connections_per_slice < 0:
        raise ValueError("min_connections_per_slice must be >= 0")
    if max_connections_per_slice is not None and max_connections_per_slice < 0:
        raise ValueError("max_connections_per_slice must be >= 0")
    if (
        max_connections_per_slice is not None
        and max_connections_per_slice < min_connections_per_slice
    ):
        raise ValueError("max_connections_per_slice must be >= min_connections_per_slice")

    parsed_min_duration = None if one_way else min_duration
    parsed_max_duration = None if one_way else max_duration
    if not one_way:
        if parsed_min_duration < 1:
            raise ValueError("min_duration must be >= 1")
        if parsed_max_duration < parsed_min_duration:
            raise ValueError("max_duration must be >= min_duration")

    return SearchConfig(
        origin=origin.upper().strip(),
        destination=destination.upper().strip(),
        target_date=target,
        date_flex_days=date_flex_days,
        one_way=one_way,
        min_duration=parsed_min_duration,
        max_duration=parsed_max_duration,
        adults=adults,
        max_results_per_query=max_results_per_query,
        min_connections_per_slice=min_connections_per_slice,
        max_connections_per_slice=max_connections_per_slice,
        output_csv=output_csv,
    )


def run_search(client: DuffelClient, config: SearchConfig) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []

    for dep_date in date_range(config.target_date, config.date_flex_days):
        if config.one_way:
            return_dates = [None]
        else:
            assert config.min_duration is not None and config.max_duration is not None
            return_dates = [
                dep_date + timedelta(days=d)
                for d in range(config.min_duration, config.max_duration + 1)
            ]

        best_for_departure: Optional[tuple[OfferSummary, date]] = None

        for ret_date in return_dates:
            try:
                offers = client.search_offers(
                    origin=config.origin,
                    destination=config.destination,
                    departure_date=dep_date,
                    return_date=ret_date,
                    adults=config.adults,
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

            summary = best_offer_summary(
                offers,
                min_connections_per_slice=config.min_connections_per_slice,
                max_connections_per_slice=config.max_connections_per_slice,
            )
            if not summary:
                continue

            if best_for_departure is None or summary.price < best_for_departure[0].price:
                best_for_departure = (summary, ret_date if ret_date else dep_date)

        row: Dict[str, str] = {
            "origin": config.origin,
            "destination": config.destination,
            "departure_date": dep_date.isoformat(),
        }
        if best_for_departure:
            summary = best_for_departure[0]
            row["best_price"] = f"{summary.price:.2f}"
            row["currency"] = summary.currency
            row["airlines"] = summary.airlines
            row["airports"] = summary.airports
            row["flight_codes"] = summary.flight_codes
            row["flight_segments"] = summary.flight_segments
            row["layovers"] = summary.layovers
            row["total_duration"] = summary.total_duration
            row["mode"] = summary.mode
            row["best_offer_id"] = summary.offer_id
            if not config.one_way:
                row["best_return_date"] = best_for_departure[1].isoformat()
        else:
            row["best_price"] = "N/A"
            row["currency"] = "N/A"
            row["airlines"] = "N/A"
            row["airports"] = "N/A"
            row["flight_codes"] = "N/A"
            row["flight_segments"] = "N/A"
            row["layovers"] = "N/A"
            row["total_duration"] = "N/A"
            row["mode"] = "N/A"
            row["best_offer_id"] = "N/A"
            if not config.one_way:
                row["best_return_date"] = "N/A"

        rows.append(row)

    return rows


def print_summary(rows: List[Dict[str, str]], one_way: bool) -> None:
    print("\nBest flight prices by departure date")
    if one_way:
        print(
            "departure_date | best_price | currency | airlines | airports | flight_codes | "
            "flight_segments | layovers | total_duration | mode | offer_id"
        )
        for row in rows:
            print(
                f"{row['departure_date']} | {row['best_price']} | "
                f"{row['currency']} | {row['airlines']} | {row['airports']} | "
                f"{row['flight_codes']} | {row['flight_segments']} | {row['layovers']} | "
                f"{row['total_duration']} | {row['mode']} | {row['best_offer_id']}"
            )
    else:
        print(
            "departure_date | best_return_date | best_price | currency | airlines | airports | "
            "flight_codes | flight_segments | layovers | total_duration | mode | offer_id"
        )
        for row in rows:
            print(
                f"{row['departure_date']} | {row['best_return_date']} | "
                f"{row['best_price']} | {row['currency']} | {row['airlines']} | "
                f"{row['airports']} | {row['flight_codes']} | {row['flight_segments']} | "
                f"{row['layovers']} | {row['total_duration']} | {row['mode']} | "
                f"{row['best_offer_id']}"
            )


def write_csv(rows: List[Dict[str, str]], csv_path: str, one_way: bool) -> None:
    fieldnames = ["origin", "destination", "departure_date"]
    if not one_way:
        fieldnames.append("best_return_date")
    fieldnames.extend(
        [
            "best_price",
            "currency",
            "airlines",
            "airports",
            "flight_codes",
            "flight_segments",
            "layovers",
            "total_duration",
            "mode",
            "best_offer_id",
        ]
    )

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search for best flights over flexible dates.")
    parser.add_argument("--origin", required=True, help="IATA origin code (e.g., MIL or MXP)")
    parser.add_argument("--destination", required=True, help="IATA destination code (e.g., LIM)")
    parser.add_argument("--target-date", required=True, help="Target departure date in YYYY-MM-DD format")
    parser.add_argument("--date-flex-days", type=int, default=30, help="Days before/after target date")
    parser.add_argument("--one-way", action="store_true", help="Search one-way flights")
    parser.add_argument("--min-duration", type=int, default=5, help="Minimum trip duration in days")
    parser.add_argument("--max-duration", type=int, default=21, help="Maximum trip duration in days")
    parser.add_argument("--adults", type=int, default=1, help="Number of adult travelers")
    parser.add_argument("--max-results-per-query", type=int, default=20, help="Max offers per API query")
    parser.add_argument("--min-connections-per-slice", type=int, default=0, help="Minimum layovers per slice")
    parser.add_argument("--max-connections-per-slice", type=int, default=2, help="Maximum layovers per slice")
    parser.add_argument("--output-csv", help="Optional path to write CSV summary")
    parser.add_argument(
        "--duffel-access-token",
        default=os.getenv("DUFFEL_ACCESS_TOKEN"),
        help="Duffel API access token (or set DUFFEL_ACCESS_TOKEN)",
    )

    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if not args.duffel_access_token:
        print("Missing API credentials. Set DUFFEL_ACCESS_TOKEN or pass --duffel-access-token.", file=sys.stderr)
        return 2

    if args.duffel_access_token.startswith("duffel_test_"):
        print(
            "Warning: using a Duffel test token. Test mode can return unrealistic schedules, prices, and flight numbers.",
            file=sys.stderr,
        )

    try:
        config = build_search_config(
            origin=args.origin,
            destination=args.destination,
            target_date=args.target_date,
            date_flex_days=args.date_flex_days,
            one_way=args.one_way,
            min_duration=args.min_duration,
            max_duration=args.max_duration,
            adults=args.adults,
            max_results_per_query=args.max_results_per_query,
            min_connections_per_slice=args.min_connections_per_slice,
            max_connections_per_slice=args.max_connections_per_slice,
            output_csv=args.output_csv,
        )
    except ValueError as err:
        print(f"Invalid arguments: {err}", file=sys.stderr)
        return 2

    client = DuffelClient(args.duffel_access_token)

    try:
        rows = run_search(client, config)
    except RateLimitError as err:
        print(f"API rate limit reached and retries exhausted. Details: {err}", file=sys.stderr)
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
