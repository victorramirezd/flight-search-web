"""Consent-aware fast-flights adapter for the application's internal offer format.

The parser exposes outbound segments only, including for round-trip searches.
Prices for those searches are indicative round-trip fares, not booked offers.
"""
from __future__ import annotations

import math
import re
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


def validate_request_interval_seconds(value):
    """Bound experiments without changing the application's normal pacing."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not 1.5 <= value <= 15 or not math.isfinite(value)):
        raise ValueError("request_interval_seconds must be a number between 1.5 and 15.")
    return float(value)


class FetchError(RuntimeError):
    """A safe explanation with no upstream response body or session data."""


def empty_diagnostics():
    return {"category": "unknown", "http_status": None, "response_kind": "unknown",
            "consent_handled": False, "elapsed_ms": 0, "exception_type": None,
            "upstream_error": False}


class ConsentAwareFetcher(FetchIntegration):
    """Adapted from the flight-search skill's bundled search_flights.py helper."""

    def __init__(self):
        self.client = Client(
            impersonate="chrome_145", impersonate_os="macos", timeout=30,
            cookie_store=True,
        )
        self.diagnostics = empty_diagnostics()

    def fetch_html(self, query):
        self.diagnostics = empty_diagnostics()
        params = query.params() if not isinstance(query, str) else {"q": query}
        url = "https://www.google.com/travel/flights"
        try:
            response = self.client.get(url, params=params)
        except Exception as exc:
            self.diagnostics.update(category="network_error", response_kind="network_error")
            raise FetchError("Google could not be reached") from exc
        self.check_status(response)
        parser = LexborHTMLParser(response.text)
        consent_forms = parser.css('form[action="https://consent.google.com/save"]')
        for form in consent_forms:
            data = {node.attributes["name"]: node.attributes.get("value", "")
                    for node in form.css("input[name]")}
            if data.get("set_eom") == "true":
                self.diagnostics.update(consent_handled=True, response_kind="consent")
                try:
                    consent = self.client.post("https://consent.google.com/save", data=data)
                except Exception as exc:
                    self.diagnostics.update(category="consent_error")
                    raise FetchError("Google consent request could not complete") from exc
                self.check_status(consent)
                try:
                    response = self.client.get(url, params=params)
                except Exception as exc:
                    self.diagnostics.update(category="network_error", response_kind="network_error")
                    raise FetchError("Google could not be reached after consent handling") from exc
                self.check_status(response)
                break
        final_parser = LexborHTMLParser(response.text)
        script = final_parser.css_first(r"script.ds\:1")
        if not script:
            has_consent = bool(final_parser.css('form[action="https://consent.google.com/save"]'))
            self.diagnostics.update(category="consent_error" if has_consent else "missing_results_script",
                                    response_kind="consent" if has_consent else "missing_results_script")
            raise FetchError("Google returned no flight-results script after consent handling")
        upstream_error = bool(re.search(r"errorHasStatus\s*:\s*true", script.text()))
        self.diagnostics.update(category="upstream_error" if upstream_error else "success",
                                response_kind="upstream_error" if upstream_error else "flights_results",
                                upstream_error=upstream_error)
        return response.text

    def check_status(self, response):
        self.diagnostics["http_status"] = response.status_code
        if response.status_code != 200:
            self.diagnostics.update(category="http_rate_limited" if response.status_code == 429 else "http_error",
                                    response_kind="http_error")
            raise FetchError(f"Google returned HTTP {response.status_code}")


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
                 exclude_self_transfers: bool = True,
                 request_interval_seconds: float = REQUEST_INTERVAL_SECONDS):
        self.request_interval_seconds = validate_request_interval_seconds(request_interval_seconds)
        self.integration = ConsentAwareFetcher()
        self.min_connections = min_connections
        self.max_connections = max_connections
        self.exclude_self_transfers = exclude_self_transfers
        self.last_request_at = None
        self.consecutive_query_errors = 0
        self.last_query_diagnostics = empty_diagnostics()
        self.last_search_url = ""

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
        self.last_search_url = query.url()
        self.integration.diagnostics = empty_diagnostics()
        # Pace date combinations; changing dates should not create a burst of requests.
        previous_request_at = self.last_request_at
        if self.last_request_at is not None:
            remaining = self.request_interval_seconds - (time.monotonic() - self.last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self.last_request_at = time.monotonic()
        start_gap_ms = (round((self.last_request_at - previous_request_at) * 1000)
                        if previous_request_at is not None else None)
        started = time.perf_counter()

        def diagnostics(category, exception=None):
            details = dict(self.integration.diagnostics)
            details.update(category=category, elapsed_ms=round((time.perf_counter() - started) * 1000),
                           exception_type=type(exception).__name__ if exception else None,
                           request_interval_seconds=self.request_interval_seconds,
                           start_gap_ms=start_gap_ms)
            self.last_query_diagnostics = details
            return details

        context = f"departure {departure_date}, return {return_date or 'one-way'}"
        try:
            results = get_flights(query, integration=self.integration)
        except FlightsNotFound as exc:
            # This is Google's generic error payload, not evidence of no availability.
            self.consecutive_query_errors += 1
            details = diagnostics("upstream_error", exc)
            details.update(upstream_error=True, response_kind="upstream_error")
            message = f"Google could not return results for {context}; availability is unknown."
            if self.consecutive_query_errors >= MAX_CONSECUTIVE_QUERY_ERRORS:
                raise ProviderUnavailableError(
                    message + f" Search stopped after {self.consecutive_query_errors} consecutive query errors.",
                    diagnostics=details, search_url=self.last_search_url,
                ) from exc
            raise ApiError(message, diagnostics=details, search_url=self.last_search_url) from exc
        except Exception as exc:
            # Do not repeat a blocked/broken scraper for every date in a flexible window.
            category = self.integration.diagnostics["category"]
            if category in {"unknown", "success", "upstream_error"}:
                category = "parser_error"
            details = diagnostics(category, exc)
            explanation = str(exc) if isinstance(exc, FetchError) else "Google response could not be parsed"
            # Preserve the safe status explanation used by older integrations.
            if re.fullmatch(r"Google returned HTTP \d{3}", str(exc)):
                status = int(str(exc).rsplit(" ", 1)[1])
                details.update(http_status=status, response_kind="http_error",
                               category="http_rate_limited" if status == 429 else "http_error")
                explanation = str(exc)
            raise ProviderUnavailableError(
                f"Google Flights search failed for {context}: {explanation}. Search stopped; availability is unknown.",
                diagnostics=details, search_url=self.last_search_url,
            ) from exc
        self.consecutive_query_errors = 0

        try:
            offers = self._normalize_offers(results, query, origin, destination, departure_date, return_date)
        except Exception as exc:
            raise ProviderUnavailableError(
                f"Google Flights search failed for {context}: Google itinerary data could not be parsed. "
                "Search stopped; availability is unknown.",
                diagnostics=diagnostics("parser_error", exc), search_url=self.last_search_url,
            ) from exc
        diagnostics("success" if offers else "no_matching_priced_results")
        # Apply stop filters before truncation, so cheap excluded fares don't hide valid ones.
        return sorted(offers, key=lambda offer: float(offer["total_amount"]))[:max_results]

    def _normalize_offers(self, results, query, origin, destination, departure_date, return_date):
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
        return offers
