#!/usr/bin/env python3
"""Flexible flight searches using consent-aware Google Flights scraping."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from functools import lru_cache
from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, List, Optional, Protocol


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
    exclude_self_transfers: bool = True
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
    airline_url: str
    provider: str
    searched_at: str
    details_note: str


class ApiError(Exception):
    """Raised when a flight query cannot return a trustworthy result."""

    def __init__(self, message: str, *, diagnostics: Optional[dict] = None,
                 search_url: str = ""):
        super().__init__(message)
        self.diagnostics = diagnostics or {}
        self.search_url = search_url


class ProviderUnavailableError(ApiError):
    """Stop the search when Google is blocked or its response cannot be parsed."""


class OfferClient(Protocol):
    def search_offers(
        self, *, origin: str, destination: str, departure_date: date,
        return_date: Optional[date], adults: int, max_results: int,
    ) -> List[dict]: ...


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
    if offer.get("_airlines"):
        return offer["_airlines"]
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
            origin = segment.get("origin")
            if isinstance(origin, dict):
                origin_label = airport_label(origin)
                if route and route[-1] != origin_label:
                    route.append(f"change airport to {origin_label}")

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
    previous_destination: Optional[str] = None

    for segment in offer_segments(offer):
        origin = segment.get("origin")
        origin_label = airport_label(origin) if isinstance(origin, dict) else "unknown"
        if previous_destination and previous_destination != origin_label:
            segment_details.append(f"Airport change: {previous_destination} -> {origin_label}")

        flight_code = segment_flight_code(segment) or "N/A"
        departing = segment.get("departing_at")
        arriving = segment.get("arriving_at")
        if departing and arriving:
            segment_details.append(f"{segment_route(segment)}: {departing.replace('T', ' ')} to {arriving.replace('T', ' ')} (local)")
        else:
            segment_details.append(f"{segment_route(segment)}: {flight_code}")

        destination = segment.get("destination")
        previous_destination = airport_label(destination) if isinstance(destination, dict) else None

    return "; ".join(segment_details) if segment_details else "N/A"


AIRLINE_WEBSITES = {
    "AA": "https://www.aa.com/",
    "AC": "https://www.aircanada.com/",
    "AF": "https://wwws.airfrance.us/",
    "AM": "https://aeromexico.com/",
    "AV": "https://www.avianca.com/",
    "BA": "https://www.britishairways.com/",
    "CM": "https://www.copaair.com/",
    "DL": "https://www.delta.com/",
    "IB": "https://www.iberia.com/",
    "KL": "https://www.klm.com/",
    "LA": "https://www.latamairlines.com/",
    "LH": "https://www.lufthansa.com/",
    "UA": "https://www.united.com/",
    "UX": "https://www.aireuropa.com/",
}


def offer_airline_url(offer: dict) -> str:
    if offer.get("_search_url"):
        return offer["_search_url"]
    for segment in offer_segments(offer):
        for carrier_key in ("marketing_carrier", "operating_carrier"):
            carrier = segment.get(carrier_key)
            code = carrier_code(carrier)
            if code and code in AIRLINE_WEBSITES:
                return AIRLINE_WEBSITES[code]

    first_airline = offer_airlines(offer)
    if first_airline and first_airline != "N/A":
        search_term = first_airline.split(",")[0].strip().replace(" ", "+")
        return f"https://www.google.com/search?q={search_term}+official+airline+website"

    return "https://www.google.com/travel/flights"


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
    if offer.get("_duration_unavailable"):
        return "N/A"
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
                airline_url=offer_airline_url(offer),
                provider=offer.get("_provider", "unknown"),
                searched_at=offer.get("_searched_at", ""),
                details_note=offer.get("_details_note", ""),
            )

    return best_summary


@lru_cache(maxsize=1)
def location_catalog() -> dict[str, dict]:
    with (Path(__file__).parent / "data" / "airports.json").open() as source:
        return {place["code"]: place for place in json.load(source)}


def known_location_codes() -> frozenset[str]:
    return frozenset(location_catalog())


def location_airport_codes(code: str) -> frozenset[str]:
    place = location_catalog()[code]
    return frozenset(place["airport_codes"]) if place["type"] == "city" else frozenset({code})


def validate_location_code(value: str, field: str) -> str:
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", code) or code not in known_location_codes():
        raise ValueError(f"{field} must be a known city or airport IATA code; choose a suggestion (for example, Palermo is PMO)")
    return code


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
    exclude_self_transfers: bool = True,
    output_csv: Optional[str] = None,
) -> SearchConfig:
    origin = validate_location_code(origin, "origin")
    destination = validate_location_code(destination, "destination")
    try:
        target = datetime.strptime(target_date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError("target_date must be in YYYY-MM-DD format") from exc

    if target - timedelta(days=max(date_flex_days, 0)) <= date.today():
        raise ValueError("The entire departure window must be in the future; adjust target_date or date_flex_days")
    if origin == destination:
        raise ValueError("origin and destination must be different airports")
    if adults > 9:
        raise ValueError("adults must be <= 9 for Google Flights")
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
        exclude_self_transfers=exclude_self_transfers,
        output_csv=output_csv,
    )


@dataclass(frozen=True)
class SearchQuery:
    departure_date: date
    return_date: Optional[date]

    def payload(self) -> dict:
        departure = self.departure_date.isoformat()
        returning = self.return_date.isoformat() if self.return_date else None
        return {"query_id": f"{departure}|{returning or 'one-way'}",
                "departure_date": departure, "return_date": returning}


def search_queries(config: SearchConfig) -> list[SearchQuery]:
    return [SearchQuery(departure, None if config.one_way else departure + timedelta(days=duration))
            for departure in date_range(config.target_date, config.date_flex_days)
            for duration in ([0] if config.one_way else range(config.min_duration, config.max_duration + 1))]


def validate_retry_queries(value, config: SearchConfig) -> list[SearchQuery]:
    if not isinstance(value, list) or not value:
        raise ValueError("retry_queries must be a non-empty list of departure/return date pairs")
    plan = search_queries(config)
    allowed = {(q.payload()["departure_date"], q.payload()["return_date"]): q for q in plan}
    selected = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each retry query must be a departure/return date pair")
        pair = (entry.get("departure_date"), entry.get("return_date"))
        if not isinstance(pair[0], str) or (pair[1] is not None and not isinstance(pair[1], str)):
            raise ValueError("Retry dates must be ISO date strings, with null return_date for one-way")
        if pair not in allowed:
            raise ValueError("Retry query is outside this search's departure/duration window")
        if pair in selected:
            raise ValueError("retry_queries must not contain duplicate date pairs")
        selected.add(pair)
    return [q for pair, q in allowed.items() if pair in selected]


def iter_search_rows(
    client: OfferClient, config: SearchConfig, *, queries: Optional[list[SearchQuery]] = None,
    on_progress: Optional[Callable[[dict], None]] = None,
) -> Iterable[Dict[str, str]]:
    plan = search_queries(config) if queries is None else queries
    completed = 0

    def progress(query: SearchQuery, status: str, message: str = "",
                 offer: Optional[OfferSummary] = None, error: Optional[ApiError] = None) -> None:
        nonlocal completed
        if status != "running":
            completed += 1
        if on_progress:
            details = {}
            if status != "running":
                diagnostics = (error.diagnostics if error else
                               getattr(client, "last_query_diagnostics", {}))
                diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
                search_url = (error.search_url if error else
                              getattr(client, "last_search_url", ""))
                defaults = {"category": "provider_error" if error else
                            ("success" if status == "ok" else "no_matching_priced_results"),
                            "http_status": None, "response_kind": "unknown", "consent_handled": False,
                            "elapsed_ms": 0, "exception_type": type(error).__name__ if error else None,
                            "upstream_error": False}
                details = {"diagnostics": {**defaults, **diagnostics},
                           "search_url": search_url if isinstance(search_url, str) else ""}
            on_progress({**query.payload(), "status": status, "message": message,
                         "completed": completed, "total": len(plan),
                         "offer": asdict(offer) if offer else None, **details})

    departures = {}
    for query in plan:
        departures.setdefault(query.departure_date, []).append(query)
    for dep_date, departure_queries in departures.items():
        return_dates = [q.return_date for q in departure_queries]

        best_for_departure: Optional[tuple[OfferSummary, date]] = None
        query_errors = []
        failed_queries = []
        stop_error = None

        for attempted, query in enumerate(departure_queries, start=1):
            ret_date = query.return_date
            progress(query, "running")
            try:
                offers = client.search_offers(
                    origin=config.origin,
                    destination=config.destination,
                    departure_date=dep_date,
                    return_date=ret_date,
                    adults=config.adults,
                    max_results=config.max_results_per_query,
                )
            except ProviderUnavailableError as err:
                progress(query, "error", str(err), error=err)
                if best_for_departure is None:
                    raise
                # Emit an explicitly partial row before terminating, preserving fares
                # from earlier return-date queries for this same departure.
                query_errors.append(str(err))
                failed_queries.append({**query.payload(), "message": str(err)})
                stop_error = err
                break
            except ApiError as err:
                progress(query, "error", str(err), error=err)
                query_errors.append(str(err))
                failed_queries.append({**query.payload(), "message": str(err)})
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
            progress(query, "ok" if summary else "no_offers", offer=summary)
            if not summary:
                continue

            if best_for_departure is None or summary.price < best_for_departure[0].price:
                best_for_departure = (summary, ret_date if ret_date else dep_date)

        row: Dict[str, str] = {
            "origin": config.origin,
            "destination": config.destination,
            "departure_date": dep_date.isoformat(),
            "failed_queries": json.dumps(failed_queries),
        }
        if query_errors:
            row["search_status"] = "partial" if best_for_departure else "error"
            row["search_message"] = (
                f"{len(query_errors)} of {len(return_dates)} date searches failed. "
                + ("Best available price may be incomplete. " if best_for_departure else "Price unavailable. ")
                + query_errors[0]
            )
            if stop_error:
                row["search_message"] += (
                    f" Search stopped after {attempted} of {len(return_dates)} combinations; "
                    f"{len(return_dates) - attempted} were not searched."
                )
        else:
            row["search_status"] = "ok" if best_for_departure else "no_offers"
            row["search_message"] = "Flights found" if best_for_departure else "Google returned no priced results matching these filters; availability is not confirmed."
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
            row["airline_url"] = summary.airline_url
            row["provider"] = summary.provider
            row["searched_at"] = summary.searched_at
            row["details_note"] = summary.details_note
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
            row["airline_url"] = ""
            row["provider"] = "google_flights"
            row["searched_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            row["details_note"] = ""
            if not config.one_way:
                row["best_return_date"] = "N/A"

        yield row
        if stop_error:
            raise stop_error


def run_search(client: OfferClient, config: SearchConfig) -> List[Dict[str, str]]:
    rows = list(iter_search_rows(client, config))
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
            "airline_url",
            "search_status",
            "search_message",
            "provider",
            "searched_at",
            "details_note",
            "failed_queries",
        ]
    )

    with open(csv_path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Search for best flights over flexible dates.")
    parser.add_argument("--origin", required=True, help="IATA airport origin code (e.g., FCO or MXP)")
    parser.add_argument("--destination", required=True, help="IATA destination code (e.g., LIM)")
    parser.add_argument("--target-date", required=True, help="Target departure date in YYYY-MM-DD format")
    parser.add_argument("--date-flex-days", type=int, default=30, help="Days before/after target date")
    parser.add_argument("--one-way", action="store_true", help="Search one-way flights")
    parser.add_argument("--min-duration", type=int, default=5, help="Minimum trip duration in days")
    parser.add_argument("--max-duration", type=int, default=21, help="Maximum trip duration in days")
    parser.add_argument("--adults", type=int, default=1, help="Number of adult travelers")
    parser.add_argument("--max-results-per-query", type=int, default=20, help="Max matching priced results per Google Flights query")
    parser.add_argument("--min-connections-per-slice", type=int, default=0, help="Minimum outbound layovers")
    parser.add_argument("--max-connections-per-slice", type=int, default=2, help="Maximum layovers each way")
    parser.add_argument("--output-csv", help="Optional path to write CSV summary")
    parser.add_argument("--include-self-transfers", action="store_true",
                        help="Include separate-ticket/self-transfer fares (excluded by default)")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

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
            exclude_self_transfers=not args.include_self_transfers,
        )
    except ValueError as err:
        print(f"Invalid arguments: {err}", file=sys.stderr)
        return 2

    from google_flights import GoogleFlightsClient
    client = GoogleFlightsClient(
        min_connections=config.min_connections_per_slice,
        max_connections=config.max_connections_per_slice,
        exclude_self_transfers=config.exclude_self_transfers,
    )

    rows = []
    search_error = None
    try:
        for row in iter_search_rows(client, config):
            rows.append(row)
    except ApiError as err:
        print(f"Search error: {err}", file=sys.stderr)
        if not rows:
            return 4
        search_error = err
        print("Retaining available results; the full search is incomplete.", file=sys.stderr)

    print("Google Flights · EUR · airport-local times · fares can change.")
    outcome = "stopped incomplete" if search_error else "completed"
    print(f"Search {outcome}: {datetime.now(timezone.utc).isoformat(timespec='seconds')} (UTC)")
    if not config.one_way:
        print("Indicative round-trip fares; details and minimum layovers cover outbound only. Confirm return flights on Google Flights.")
    print_summary(rows, one_way=config.one_way)
    for row in rows:
        if row["search_status"] != "ok":
            print(f"{row['departure_date']}: {row['search_message']}")

    if config.output_csv:
        write_csv(rows, config.output_csv, one_way=config.one_way)
        print(f"\nCSV summary written to: {config.output_csv}")

    return 4 if search_error else 0


if __name__ == "__main__":
    # The provider imports these exception types. Keep them identical in CLI runs.
    sys.modules.setdefault("flight_search", sys.modules[__name__])
    raise SystemExit(main())
