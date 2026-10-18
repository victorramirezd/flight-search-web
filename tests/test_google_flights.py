import csv
import io
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fast_flights.model import Airport, CarbonEmission, Flights, SimpleDatetime, SingleFlight
from fast_flights.exceptions import FlightsNotFound

from flight_search import (ApiError, ProviderUnavailableError, best_offer_summary, build_search_config,
                           iter_search_rows, main, write_csv)
from google_flights import ConsentAwareFetcher, GoogleFlightsClient, itinerary_duration


DEPARTURE = date.today() + timedelta(days=30)


def leg(origin="FCO", destination="LHR", departure=(20, 30), arrival=(22, 10), duration=160):
    day = (DEPARTURE.year, DEPARTURE.month, DEPARTURE.day)
    return SingleFlight(Airport(origin, origin), Airport(destination, destination),
                        SimpleDatetime(day, departure), SimpleDatetime(day, arrival), duration, "A320")


def itinerary(price=98, legs=None):
    return Flights("BA", price, ["British Airways"], [leg()] if legs is None else legs,
                   CarbonEmission(123000, 112000))


class GoogleFlightsTest(unittest.TestCase):
    def search(self, client, **changes):
        args = dict(origin="FCO", destination="LHR", departure_date=DEPARTURE,
                    return_date=None, adults=1, max_results=20)
        args.update(changes)
        return client.search_offers(**args)

    @patch("google_flights.get_flights")
    def test_round_trip_passengers_currency_and_stop_filters_reach_query(self, fetch):
        fetch.return_value = [itinerary()]
        client = GoogleFlightsClient(max_connections=1)
        offers = self.search(client, return_date=DEPARTURE + timedelta(days=7), adults=2)
        query = fetch.call_args.args[0]
        self.assertEqual(query.get_trip_type(), "round-trip")
        self.assertEqual(query.currency, "EUR")
        self.assertEqual(len(query.passengers), 2)
        self.assertEqual([(f.from_airport.airport, f.to_airport.airport) for f in query.flight_data],
                         [("FCO", "LHR"), ("LHR", "FCO")])
        self.assertEqual([f.max_stops for f in query.flight_data], [1, 1])
        self.assertIs(fetch.call_args.kwargs["integration"], client.integration)
        summary = best_offer_summary(offers)
        self.assertEqual(summary.price, 98)
        self.assertEqual(summary.flight_codes, "N/A")
        self.assertEqual(summary.total_duration, "2h 40m")  # Different airport timezones.
        self.assertIn("20:30", summary.flight_segments)
        self.assertIn("22:10", summary.flight_segments)
        self.assertIn("round-trip", summary.details_note)
        self.assertIn("outbound only", summary.details_note)
        self.assertTrue(summary.airline_url.startswith("https://www.google.com/travel/flights/search?tfs="))
        self.assertEqual(summary.provider, "google_flights")
        self.assertTrue(summary.searched_at.endswith("+00:00"))

    @patch("google_flights.get_flights")
    def test_filter_before_limit_skip_unpriced_and_sort_both_result_groups(self, fetch):
        connecting = [leg("FCO", "CDG", (8, 0), (10, 0), 120),
                      leg("CDG", "LHR", (11, 0), (11, 30), 90)]
        fetch.return_value = [itinerary(1), itinerary(None), itinerary(150, connecting), itinerary(90, connecting)]
        offers = self.search(GoogleFlightsClient(min_connections=1), max_results=1)
        self.assertEqual(len(offers), 1)
        summary = best_offer_summary(offers, min_connections_per_slice=1)
        self.assertEqual(summary.price, 90)
        self.assertEqual(summary.total_duration, "4h 30m")
        self.assertEqual(summary.layovers, "1 (CDG)")

    @patch("google_flights.get_flights", side_effect=RuntimeError("Google returned HTTP 429"))
    def test_blocked_search_stops_flexible_window_without_retry(self, fetch):
        config = build_search_config(origin="FCO", destination="LHR", target_date=DEPARTURE.isoformat(),
                                     date_flex_days=2, one_way=True)
        with self.assertRaisesRegex(ProviderUnavailableError, "availability is unknown"):
            list(iter_search_rows(GoogleFlightsClient(), config))
        self.assertEqual(fetch.call_count, 1)

    @patch("google_flights.time.sleep")
    @patch("google_flights.get_flights")
    def test_isolated_google_error_is_partial_and_other_returns_are_searched(self, fetch, sleep):
        fetch.side_effect = [[itinerary(150)], FlightsNotFound("no flights found; received error"),
                             [itinerary(90)]]
        config = build_search_config(origin="FCO", destination="LHR", target_date=DEPARTURE.isoformat(),
                                     date_flex_days=0, min_duration=12, max_duration=14)
        rows = list(iter_search_rows(GoogleFlightsClient(), config))
        self.assertEqual(fetch.call_count, 3)
        self.assertEqual(rows[0]["search_status"], "partial")
        self.assertEqual(rows[0]["best_price"], "90.00")
        self.assertEqual(rows[0]["best_return_date"], (DEPARTURE + timedelta(days=14)).isoformat())
        self.assertIn("1 of 3", rows[0]["search_message"])
        self.assertIn((DEPARTURE + timedelta(days=13)).isoformat(), rows[0]["search_message"])

    @patch("google_flights.time.sleep")
    @patch("google_flights.get_flights", side_effect=FlightsNotFound("no flights found; received error"))
    def test_generic_errors_are_bounded_and_do_not_claim_no_availability(self, fetch, sleep):
        client = GoogleFlightsClient()
        for duration in (12, 13):
            with self.assertRaises(ApiError) as error:
                self.search(client, return_date=DEPARTURE + timedelta(days=duration))
            self.assertNotIsInstance(error.exception, ProviderUnavailableError)
            self.assertIn("availability is unknown", str(error.exception))
        with self.assertRaisesRegex(ProviderUnavailableError, "3 consecutive query errors"):
            self.search(client, return_date=DEPARTURE + timedelta(days=14))
        returns = [call.args[0].flight_data[1].date for call in fetch.call_args_list]
        self.assertEqual(len(set(returns)), 3)  # No failed combination is retried.

    @patch("google_flights.time.sleep")
    @patch("google_flights.get_flights")
    def test_success_resets_consecutive_error_count(self, fetch, sleep):
        fetch.side_effect = [FlightsNotFound("error"), [], FlightsNotFound("error"),
                             FlightsNotFound("error"), []]
        client = GoogleFlightsClient()
        for duration, fails in enumerate((True, False, True, True, False), start=12):
            if fails:
                with self.assertRaises(ApiError) as error:
                    self.search(client, return_date=DEPARTURE + timedelta(days=duration))
                self.assertNotIsInstance(error.exception, ProviderUnavailableError)
            else:
                self.assertEqual(self.search(client, return_date=DEPARTURE + timedelta(days=duration)), [])

    @patch("google_flights.time.sleep")
    @patch("google_flights.get_flights")
    def test_fatal_error_preserves_current_departure_fare_as_partial(self, fetch, sleep):
        fetch.side_effect = [[itinerary()], RuntimeError("Google returned HTTP 429")]
        config = build_search_config(origin="FCO", destination="LHR", target_date=DEPARTURE.isoformat(),
                                     date_flex_days=0, min_duration=12, max_duration=14)
        rows = iter(iter_search_rows(GoogleFlightsClient(), config))
        row = next(rows)
        self.assertEqual(row["best_price"], "98.00")
        self.assertEqual(row["search_status"], "partial")
        self.assertIn("2 of 3 combinations", row["search_message"])
        self.assertIn("1 were not searched", row["search_message"])
        with self.assertRaisesRegex(ProviderUnavailableError, "HTTP 429"):
            next(rows)
        self.assertEqual(fetch.call_count, 2)

    @patch("google_flights.get_flights", return_value=[])
    @patch("google_flights.time.monotonic", side_effect=[100, 100.25, 104.0])
    @patch("google_flights.time.sleep")
    def test_requests_are_paced(self, sleep, clock, fetch):
        client = GoogleFlightsClient()
        self.search(client)
        self.search(client, return_date=DEPARTURE + timedelta(days=7))
        sleep.assert_called_once_with(3.75)

    @patch("google_flights.get_flights", return_value=[])
    def test_configured_intervals_pace_request_starts_and_record_actual_gap(self, fetch):
        for interval in (1.5, 4, 8):
            with self.subTest(interval=interval), patch("google_flights.time.sleep") as sleep, \
                    patch("google_flights.time.monotonic", side_effect=[100, 100.25, 100 + interval]):
                client = GoogleFlightsClient(request_interval_seconds=interval)
                self.search(client)
                self.assertIsNone(client.last_query_diagnostics["start_gap_ms"])
                self.search(client, return_date=DEPARTURE + timedelta(days=7))
                sleep.assert_called_once_with(interval - 0.25)
                self.assertEqual(client.request_interval_seconds, interval)
                self.assertEqual(client.last_query_diagnostics["request_interval_seconds"], interval)
                self.assertEqual(client.last_query_diagnostics["start_gap_ms"], interval * 1000)

    @patch("google_flights.get_flights", return_value=[])
    def test_elapsed_slow_response_needs_no_additional_wait(self, fetch):
        for interval in (1.5, 4, 8):
            with self.subTest(interval=interval), patch("google_flights.time.sleep") as sleep, \
                    patch("google_flights.time.monotonic", side_effect=[100, 110, 110]):
                client = GoogleFlightsClient(request_interval_seconds=interval)
                self.search(client)
                self.search(client, return_date=DEPARTURE + timedelta(days=7))
                sleep.assert_not_called()
                self.assertEqual(client.last_query_diagnostics["start_gap_ms"], 10000)

    def test_request_interval_constructor_validation_before_creating_session(self):
        invalid = [True, False, None, "4", "", {}, [], float("nan"), float("inf"),
                   -float("inf"), 0, -1, 1.499, 15.001, 10 ** 1000]
        with patch("google_flights.ConsentAwareFetcher") as fetcher:
            for value in invalid:
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, "between 1.5 and 15"):
                    GoogleFlightsClient(request_interval_seconds=value)
            fetcher.assert_not_called()
        self.assertEqual(GoogleFlightsClient().request_interval_seconds, 4.0)
        self.assertEqual(GoogleFlightsClient(request_interval_seconds=15).request_interval_seconds, 15)

    @patch("google_flights.time.sleep")
    @patch("google_flights.get_flights")
    def test_cli_exports_partial_results_and_exits_nonzero_when_stopped(self, fetch, sleep):
        fetch.side_effect = [[itinerary()], RuntimeError("Google returned HTTP 429")]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "partial.csv"
            argv = ["flight_search.py", "--origin", "FCO", "--destination", "LHR",
                    "--target-date", DEPARTURE.isoformat(), "--date-flex-days", "0",
                    "--min-duration", "12", "--max-duration", "14", "--output-csv", str(path)]
            with patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO) as out, \
                    patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(main(), 4)
            self.assertIn("Search stopped incomplete", out.getvalue())
            with path.open() as source:
                row = next(csv.DictReader(source))
                self.assertEqual(row["best_price"], "98.00")
                self.assertEqual(row["search_status"], "partial")

    @patch("google_flights.get_flights", return_value=[])
    def test_empty_search_is_cautious_and_csv_includes_status(self, fetch):
        config = build_search_config(origin="FCO", destination="LHR", target_date=DEPARTURE.isoformat(),
                                     date_flex_days=0, one_way=True)
        rows = list(iter_search_rows(GoogleFlightsClient(), config))
        self.assertEqual(rows[0]["search_status"], "no_offers")
        self.assertIn("availability is not confirmed", rows[0]["search_message"])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "results.csv"
            write_csv(rows, str(path), one_way=True)
            with path.open() as source:
                self.assertEqual(next(csv.DictReader(source))["search_status"], "no_offers")

    def test_airport_changes_do_not_invent_elapsed_duration(self):
        self.assertIsNone(itinerary_duration([leg("FCO", "CDG"), leg("ORY", "LHR")]))

    def test_past_windows_and_too_many_passengers_are_rejected(self):
        defaults = dict(origin="FCO", destination="LHR", target_date=DEPARTURE.isoformat(),
                        date_flex_days=0, one_way=True)
        for changes in ({"adults": 10}, {"destination": "FCO"},
                        {"target_date": date.today().isoformat()}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                build_search_config(**(defaults | changes))

    @patch("google_flights.get_flights")
    def test_city_endpoints_accept_members_and_preserve_actual_airports(self, fetch):
        fetch.return_value = [itinerary(100, [leg("LIN", "LHR")]),
                             itinerary(90, [leg("BGY", "STN")]),
                             itinerary(80, [leg("FCO", "LHR")]),
                             itinerary(70, [leg("MXP", "CDG")])]
        offers = self.search(GoogleFlightsClient(), origin="MIL", destination="LON")
        self.assertEqual(len(offers), 2)
        self.assertEqual(best_offer_summary(offers).airports, "BGY -> STN")
        query = fetch.call_args.args[0]
        self.assertEqual(query.flight_data[0].from_airport.airport, "MIL")
        self.assertEqual(query.flight_data[0].to_airport.airport, "LON")

    @patch("google_flights.get_flights")
    def test_airport_to_city_keeps_exact_origin(self, fetch):
        fetch.return_value = [itinerary(90, [leg("FCO", "LHR")]), itinerary(80, [leg("CIA", "STN")])]
        offers = self.search(GoogleFlightsClient(), destination="LON")
        self.assertEqual(len(offers), 1)
        self.assertEqual(best_offer_summary(offers).airports, "FCO -> LHR")

    @patch("google_flights.get_flights", return_value=[])
    def test_transfer_filter_is_in_query_and_google_link(self, fetch):
        from base64 import b64decode
        from urllib.parse import parse_qs, urlparse
        from fast_flights.pb.flights_pb2 import Info
        for excluded in (True, False):
            self.search(GoogleFlightsClient(exclude_self_transfers=excluded))
            query = fetch.call_args.args[0]
            self.assertEqual(query.hide_separate_and_self_transfer, excluded)
            encoded = parse_qs(urlparse(query.url()).query)["tfs"][0]
            self.assertEqual(Info.FromString(b64decode(encoded)).hide_separate_and_self_transfer, excluded)

    @patch("google_flights.get_flights", return_value=[])
    def test_cli_can_include_self_transfers(self, fetch):
        argv = ["flight_search.py", "--origin", "MIL", "--destination", "LON", "--target-date",
                DEPARTURE.isoformat(), "--date-flex-days", "0", "--one-way", "--include-self-transfers"]
        with patch("sys.argv", argv), patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(main(), 0)
        self.assertFalse(fetch.call_args.args[0].hide_separate_and_self_transfer)


class ConsentTest(unittest.TestCase):
    def fetcher(self, responses):
        fetcher = ConsentAwareFetcher()
        fetcher.client = Mock()
        fetcher.client.get.side_effect = responses
        fetcher.client.post.return_value = SimpleNamespace(status_code=200)
        return fetcher

    def test_rejects_optional_cookies_and_reuses_session_and_query(self):
        consent = '''<form action="https://consent.google.com/save">
            <input name="set_eom" value="false"></form>
            <form action="https://consent.google.com/save">
            <input name="set_eom" value="true"><input name="token" value="example"></form>'''
        results = '<script class="ds:1">data</script>'
        fetcher = self.fetcher([SimpleNamespace(status_code=200, text=consent),
                                SimpleNamespace(status_code=200, text=results)])
        query = Mock()
        query.params.return_value = {"tfs": "query", "curr": "EUR"}
        self.assertEqual(fetcher.fetch_html(query), results)
        self.assertEqual(fetcher.client.post.call_args.kwargs["data"],
                         {"set_eom": "true", "token": "example"})
        self.assertEqual(fetcher.client.get.call_args_list[0], fetcher.client.get.call_args_list[1])

    def test_missing_results_and_http_failure_are_errors(self):
        for response in [SimpleNamespace(status_code=200, text="<html>Blocked</html>"),
                         SimpleNamespace(status_code=429, text="")]:
            with self.subTest(status=response.status_code), self.assertRaises(RuntimeError):
                self.fetcher([response]).fetch_html("query")

    def test_fetcher_classifies_http_missing_consent_and_network_failures(self):
        cases = [(429, "", "http_rate_limited", "http_error"),
                 (503, "", "http_error", "http_error"),
                 (200, "<html>Unrecognized page</html>", "missing_results_script", "missing_results_script"),
                 (200, '<form action="https://consent.google.com/save"></form>', "consent_error", "consent")]
        for status, text, category, kind in cases:
            with self.subTest(category=category):
                fetcher = self.fetcher([SimpleNamespace(status_code=status, text=text)])
                with self.assertRaises(RuntimeError):
                    fetcher.fetch_html("query")
                self.assertEqual(fetcher.diagnostics["category"], category)
                self.assertEqual(fetcher.diagnostics["response_kind"], kind)
                self.assertEqual(fetcher.diagnostics["http_status"], status)
        fetcher = self.fetcher([OSError("private network details")])
        with self.assertRaisesRegex(RuntimeError, "Google could not be reached"):
            fetcher.fetch_html("query")
        self.assertEqual(fetcher.diagnostics["category"], "network_error")
        self.assertIsNone(fetcher.diagnostics["http_status"])

    def test_consent_and_generic_payload_diagnostics_reset_between_queries(self):
        consent = '<form action="https://consent.google.com/save"><input name="set_eom" value="true"></form>'
        generic = '<script class="ds:1">data:errorHasStatus: true,</script>'
        results = '<script class="ds:1">data:[],</script>'
        fetcher = self.fetcher([SimpleNamespace(status_code=200, text=consent),
                                SimpleNamespace(status_code=200, text=generic),
                                SimpleNamespace(status_code=200, text=results)])
        fetcher.fetch_html("query")
        self.assertEqual(fetcher.diagnostics["category"], "upstream_error")
        self.assertTrue(fetcher.diagnostics["upstream_error"])
        self.assertTrue(fetcher.diagnostics["consent_handled"])
        fetcher.fetch_html("query")
        self.assertFalse(fetcher.diagnostics["upstream_error"])
        self.assertFalse(fetcher.diagnostics["consent_handled"])
        self.assertEqual(fetcher.diagnostics["response_kind"], "flights_results")

    @patch("google_flights.time.sleep")
    def test_real_fetcher_and_parser_classify_error_progress_without_response_secrets(self, sleep):
        cfg = build_search_config(origin="MIL", destination="PMO", target_date=DEPARTURE.isoformat(),
                                  date_flex_days=0, min_duration=2, max_duration=4)
        generic = '<script class="ds:1">data:errorHasStatus: true,</script>'
        malformed = '<script class="ds:1">SECRET_COOKIE_AND_BODY</script>'
        client = GoogleFlightsClient()
        client.integration = self.fetcher([SimpleNamespace(status_code=200, text=generic),
                                           SimpleNamespace(status_code=200, text=malformed)])
        progress = []
        with self.assertRaises(ProviderUnavailableError) as raised:
            list(iter_search_rows(client, cfg, on_progress=progress.append))
        finished = [event for event in progress if event["status"] == "error"]
        self.assertEqual([event["diagnostics"]["category"] for event in finished],
                         ["upstream_error", "parser_error"])
        self.assertEqual(finished[0]["diagnostics"]["exception_type"], "FlightsNotFound")
        self.assertEqual(finished[1]["diagnostics"]["response_kind"], "flights_results")
        self.assertEqual([event["completed"] for event in finished], [1, 2])
        self.assertNotEqual(finished[0]["search_url"], finished[1]["search_url"])
        for event in finished:
            self.assertEqual(event["diagnostics"]["http_status"], 200)
            self.assertGreaterEqual(event["diagnostics"]["elapsed_ms"], 0)
            self.assertTrue(event["search_url"].startswith("https://www.google.com/travel/flights/search?"))
        self.assertNotIn("SECRET_COOKIE_AND_BODY", str(raised.exception))

    @patch("google_flights.get_flights", return_value=[SimpleNamespace(price="broken-secret")])
    def test_malformed_itinerary_stops_safely_with_diagnostics(self, fetch):
        client = GoogleFlightsClient()
        with self.assertRaises(ProviderUnavailableError) as error:
            client.search_offers(origin="MIL", destination="PMO", departure_date=DEPARTURE,
                                 return_date=None, adults=1, max_results=20)
        self.assertEqual(error.exception.diagnostics["category"], "parser_error")
        self.assertNotIn("broken-secret", str(error.exception))

    @patch("google_flights.get_flights", return_value=[])
    def test_empty_priced_results_get_successful_empty_diagnostics(self, fetch):
        client = GoogleFlightsClient()
        client.search_offers(origin="MIL", destination="PMO", departure_date=DEPARTURE,
                             return_date=None, adults=1, max_results=20)
        self.assertEqual(client.last_query_diagnostics["category"], "no_matching_priced_results")
        self.assertIsNone(client.last_query_diagnostics["exception_type"])
