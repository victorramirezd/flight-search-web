"""Consent-aware fast-flights adapter for the application's internal offer format.

The parser exposes outbound segments only, including for round-trip searches.
Prices for those searches are indicative round-trip fares, not booked offers.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timezone
from typing import Optional

from fast_flights import FlightQuery, Passengers, create_query, get_flights
from fast_flights.exceptions import FlightsNotFound
from fast_flights.integrations.base import FetchIntegration
from primp import Client
from selectolax.lexbor import LexborHTMLParser

from flight_search import ApiError, ProviderUnavailableError, location_airport_codes

REQUEST_INTERVAL_SECONDS = 1.5
MAX_CONSECUTIVE_QUERY_ERRORS = 3


class ConsentAwareFetcher(FetchIntegration):
    """Adapted from the flight-search skill's bundled search_flights.py helper."""

    def __init__(self):
        self.client = Client(
            impersonate="chrome_145", impersonate_os="macos", timeout=30,
            cookie_store=True,
        )

    def fetch_html(self, query):
        params = query.params() if not isinstance(query, str) else {"q": query}
        url = "https://www.google.com/travel/flights"
        response = self.client.get(url, params=params)
        self.check_status(response)
        parser = LexborHTMLParser(response.text)
        for form in parser.css('form[action="https://consent.google.com/save"]'):
            data = {node.attributes["name"]: node.attributes.get("value", "")
                    for node in form.css("input[name]")}
            if data.get("set_eom") == "true":
                consent = self.client.post("https://consent.google.com/save", data=data)
                self.check_status(consent)
                response = self.client.get(url, params=params)
                self.check_status(response)
                break
        if not LexborHTMLParser(response.text).css_first(r"script.ds\:1"):
            raise RuntimeError("Google returned no flight-results script after consent handling")
        return response.text

    @staticmethod
    def check_status(response):
        if response.status_code != 200:
            raise RuntimeError(f"Google returned HTTP {response.status_code}")


def local_datetime(value) -> datetime:
    """Google supplies airport-local wall times without timezone offsets."""
    return datetime(*value.date, *value.time)


def itinerary_duration(legs) -> Optional[int]:
    minutes = sum(leg.duration for leg in legs)
    for previous, current in zip(legs, legs[1:]):
        # Different airports can have different timezones. Do not guess a transfer.
        if previous.to_airport.code != current.from_airport.code:
            return None
        connection = local_datetime(current.departure) - local_datetime(previous.arrival)
        if connection.total_seconds() < 0:
            return None
        minutes += int(connection.total_seconds() / 60)
    return minutes


class GoogleFlightsClient:
    def __init__(self, *, min_connections: int = 0, max_connections: Optional[int] = 2,
                 exclude_self_transfers: bool = True):
        self.integration = ConsentAwareFetcher()
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.exclude_self_transfers = exclude_self_transfers
        self.last_request_at = None
        self.consecutive_query_errors = 0

    def search_offers(
        self, *, origin: str, destination: str, departure_date: date,
        return_date: Optional[date], adults: int, max_results: int,
    ) -> list[dict]:
        flights = [FlightQuery(date=departure_date.isoformat(), from_airport=origin,
                               to_airport=destination)]
        if return_date:
            flights.append(FlightQuery(date=return_date.isoformat(), from_airport=destination,
                                       to_airport=origin))
        query = create_query(
            flights=flights, passengers=Passengers(adults=adults), seat="economy",
            trip="round-trip" if return_date else "one-way", currency="EUR", language="en",
            max_stops=self.max_connections,
            hide_separate_and_self_transfer=self.exclude_self_transfers,
        )
        # Pace date combinations; changing dates should not create a burst of requests.
        if self.last_request_at is not None:
            remaining = REQUEST_INTERVAL_SECONDS - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()
        context = f"departure {departure_date}, return {return_date or 'one-way'}"
        try:
            results = get_flights(query, integration=self.integration)
        except FlightsNotFound as exc:
            # This is Google's generic error payload, not evidence of no availability.
            self.consecutive_query_errors += 1
            message = f"Google could not return results for {context}; availability is unknown."
            if self.consecutive_query_errors >= MAX_CONSECUTIVE_QUERY_ERRORS:
                raise ProviderUnavailableError(
                    message + f" Search stopped after {self.consecutive_query_errors} consecutive query errors."
                ) from exc
            raise ApiError(message) from exc
        except Exception as exc:
            # Do not repeat a blocked/broken scraper for every date in a flexible window.
            raise ProviderUnavailableError(
                f"Google Flights search failed for {context}: {exc}. Search stopped; availability is unknown."
            ) from exc
        self.consecutive_query_errors = 0

        searched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        offers = []
        for result in results:
            if result.price is None or not math.isfinite(result.price) or result.price < 0:
                continue
            legs = result.flights
            if not legs:
                continue
            if (legs[0].from_airport.code not in location_airport_codes(origin)
                    or legs[-1].to_airport.code not in location_airport_codes(destination)
                    or local_datetime(legs[0].departure).date() != departure_date):
                continue
            stops = len(legs) - 1
            if stops < self.min_connections:
                continue
            if self.max_connections is not None and stops > self.max_connections:
                continue
            segments = [{
                "origin": {"iata_code": leg.from_airport.code},
                "destination": {"iata_code": leg.to_airport.code},
                "departing_at": local_datetime(leg.departure).isoformat(timespec="minutes"),
                "arriving_at": local_datetime(leg.arrival).isoformat(timespec="minutes"),
            } for leg in legs]
            duration = itinerary_duration(legs)
            note = (
                "Indicative round-trip fare; itinerary and minimum layover filter cover outbound only. "
                "Choose return flights on Google Flights. " if return_date else ""
            )
            offers.append({
                "total_amount": str(result.price), "total_currency": "EUR", "live_mode": True,
                "slices": [{"segments": segments, "duration": f"PT{duration}M" if duration is not None else ""}],
                "_duration_unavailable": duration is None,
                "_airlines": ", ".join(result.airlines) or "N/A",
                "_search_url": query.url(), "_provider": "google_flights", "_searched_at": searched_at,
                "_details_note": note + "Times are local to each airport. Flight numbers are unavailable. Fares can change.",
            })
        # Apply stop filters before truncation, so cheap excluded fares don't hide valid ones.
        return sorted(offers, key=lambda offer: float(offer["total_amount"]))[:max_results]
