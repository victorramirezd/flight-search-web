import json
import io
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from api.search import handler
from flight_search import (ApiError, build_search_config, iter_search_rows, search_queries,
                           validate_retry_queries)

DEPARTURE = date.today() + timedelta(days=30)


def config(**changes):
    args = dict(origin="MIL", destination="LON", target_date=DEPARTURE.isoformat(),
                date_flex_days=0, min_duration=12, max_duration=17)
    return build_search_config(**(args | changes))


@contextmanager
def endpoint():
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def post(address, payload):
    connection = HTTPConnection(*address)
    connection.request("POST", "/api/search", json.dumps(payload), {"Content-Type": "application/json"})
    response = connection.getresponse()
    body = response.read()
    status = response.status
    connection.close()
    return status, body


class SearchQueriesTest(unittest.TestCase):
    def test_cli_entrypoint_handles_provider_exception_without_traceback(self):
        code = '''import runpy, sys, types
class Client:
    def __init__(self, **kwargs): pass
    def search_offers(self, **kwargs):
        from flight_search import ProviderUnavailableError
        raise ProviderUnavailableError('Google returned HTTP 429')
sys.modules['google_flights'] = types.SimpleNamespace(GoogleFlightsClient=Client)
from datetime import date, timedelta
sys.argv = ['flight_search.py', '--origin', 'MIL', '--destination', 'LON',
            '--target-date', (date.today()+timedelta(days=30)).isoformat(),
            '--date-flex-days', '0', '--one-way']
runpy.run_module('flight_search', run_name='__main__', alter_sys=True)
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 4)
        self.assertIn("Search error: Google returned HTTP 429", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_city_config_defaults_and_ordered_grid(self):
        cfg = config(date_flex_days=4)
        self.assertTrue(cfg.exclude_self_transfers)
        queries = search_queries(cfg)
        self.assertEqual(len(queries), 54)
        self.assertEqual(queries[0].departure_date, DEPARTURE - timedelta(days=4))
        self.assertEqual(queries[0].return_date, DEPARTURE + timedelta(days=8))
        self.assertEqual(queries[-1].return_date, DEPARTURE + timedelta(days=21))

    def test_retry_validation_rejects_duplicates_invalid_and_outside_pairs(self):
        cfg = config()
        valid = search_queries(cfg)[0].payload()
        for invalid in (None, [], {}, [valid, valid], ["bad"],
                        [{"departure_date": "1900-01-01", "return_date": None}],
                        [{"departure_date": DEPARTURE.isoformat(), "return_date": []}],
                        [{"departure_date": DEPARTURE.isoformat(), "return_date": None}]):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                validate_retry_queries(invalid, cfg)

    def test_subset_is_searched_once_in_canonical_order_with_progress(self):
        from unittest.mock import Mock
        cfg = config()
        plan = search_queries(cfg)
        selected = validate_retry_queries([plan[4].payload(), plan[1].payload()], cfg)
        client = Mock()
        client.search_offers.return_value = []
        progress = []
        rows = list(iter_search_rows(client, cfg, queries=selected, on_progress=progress.append))
        self.assertEqual(client.search_offers.call_count, 2)
        self.assertEqual([call.kwargs["return_date"] for call in client.search_offers.call_args_list],
                         [plan[1].return_date, plan[4].return_date])
        self.assertEqual([p["status"] for p in progress], ["running", "no_offers", "running", "no_offers"])
        self.assertEqual([p["completed"] for p in progress], [0, 1, 1, 2])
        self.assertTrue(all(p["total"] == 2 for p in progress))
        self.assertEqual(len(rows), 1)

    def test_all_isolated_failures_are_preserved(self):
        from unittest.mock import Mock
        client = Mock()
        client.search_offers.side_effect = ApiError("Query unavailable")
        cfg = config(min_duration=12, max_duration=13)
        progress = []
        row = next(iter(iter_search_rows(client, cfg, on_progress=progress.append)))
        self.assertEqual(len(json.loads(row["failed_queries"])), 2)
        self.assertEqual(sum(p["status"] == "error" for p in progress), 2)
        self.assertEqual(row["search_status"], "error")

    def test_one_way_retry_requires_null_return_date(self):
        cfg = config(one_way=True)
        query = search_queries(cfg)[0]
        self.assertEqual(validate_retry_queries([query.payload()], cfg), [query])
        self.assertIsNone(query.payload()["return_date"])


class RetryEndpointTest(unittest.TestCase):
    def test_request_interval_is_validated_before_stream_or_client_creation(self):
        payload = dict(password="Victor", origin="MIL", destination="PMO", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=2, max_duration=4)
        invalid = [True, False, None, "4", "", {}, [], float("nan"), float("inf"),
                   -float("inf"), 0, -1, 1.499, 15.001, 10 ** 1000]
        with endpoint() as address, patch("api.search.GoogleFlightsClient") as Client, \
                patch("api.search.sys.stderr", new_callable=io.StringIO):
            for value in invalid:
                with self.subTest(value=value):
                    status, body = post(address, {**payload, "request_interval_seconds": value})
                    self.assertEqual(status, 400)
                    self.assertIn("request_interval_seconds", json.loads(body)["error"])
                    self.assertNotIn(b'"type": "start"', body)
            Client.assert_not_called()

    def test_intervals_pass_through_original_and_saved_targeted_retry_with_metadata(self):
        payload = dict(password="Victor", origin="MIL", destination="LON", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=12, max_duration=17)
        pair = search_queries(config())[2].payload()
        with endpoint() as address, patch("api.search.GoogleFlightsClient") as Client, \
                patch("api.search.sys.stderr", new_callable=io.StringIO) as logs:
            Client.return_value.search_offers.return_value = []
            for interval in (1.5, 4, 8):
                saved = {**payload, "request_interval_seconds": interval}
                for is_retry in (False, True):
                    with self.subTest(interval=interval, retry=is_retry):
                        request = {**saved, "retry_queries": [pair]} if is_retry else saved
                        status, body = post(address, request)
                        self.assertEqual(status, 200)
                        events = [json.loads(line) for line in body.splitlines()]
                        self.assertEqual(Client.call_args.kwargs["request_interval_seconds"], interval)
                        self.assertEqual(events[0]["request_interval_seconds"], interval)
                        self.assertEqual(events[-1]["request_interval_seconds"], interval)
                        self.assertEqual(events[0]["is_retry"], is_retry)
                        completed = [event for event in events
                                     if event["type"] == "progress" and event["status"] != "running"]
                        self.assertTrue(completed)
                        self.assertTrue(all(event["diagnostics"]["request_interval_seconds"] == interval
                                            for event in completed))
                        if is_retry:
                            self.assertEqual(events[0]["queries"], [pair])
            self.assertIn('"request_interval_seconds": 8.0', logs.getvalue())

    def test_stream_diagnostics_correlate_with_safe_logs_and_preserve_pending_queries(self):
        from types import SimpleNamespace
        payload = dict(password="Victor", origin="MIL", destination="PMO", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=2, max_duration=4)
        network = Mock()
        generic = '<script class="ds:1">data:errorHasStatus: true,</script>'
        network.get.side_effect = [SimpleNamespace(status_code=200, text=generic),
                                   SimpleNamespace(status_code=429, text="SECRET_UPSTREAM_BODY")]
        with endpoint() as address, patch("google_flights.Client", return_value=network), \
                patch("google_flights.time.sleep"), patch("api.search.sys.stderr", new_callable=io.StringIO) as logs, \
                patch.dict("os.environ", {"VERCEL_REGION": "fra1", "VERCEL_GIT_COMMIT_SHA": "a" * 40}):
            status, body = post(address, payload)
            safe_logs = logs.getvalue()
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in body.splitlines()]
        start, terminal = events[0], events[-1]
        self.assertEqual(terminal["type"], "error")
        self.assertEqual(terminal["request_id"], start["request_id"])
        self.assertEqual(start["region"], "fra1")
        self.assertEqual(start["revision"], "a" * 40)
        self.assertEqual(terminal["diagnostics"]["category"], "http_rate_limited")
        self.assertEqual(terminal["diagnostics"]["http_status"], 429)
        self.assertEqual(start["request_interval_seconds"], 1.5)
        self.assertEqual(terminal["request_interval_seconds"], 1.5)
        self.assertEqual(terminal["diagnostics"]["request_interval_seconds"], 1.5)
        self.assertEqual(terminal["completed"], 2)
        self.assertEqual(len(terminal["failed_queries"]), 2)
        self.assertEqual(len(terminal["pending_queries"]), 1)
        self.assertEqual([event["diagnostics"]["category"] for event in events
                          if event["type"] == "progress" and event["status"] == "error"],
                         ["upstream_error", "http_rate_limited"])
        self.assertNotIn("SECRET_UPSTREAM_BODY", body.decode() + safe_logs)
        self.assertNotIn('"password"', safe_logs)
        self.assertIn(start["request_id"], safe_logs)
        self.assertIn('"event": "flight_query"', safe_logs)

    def test_each_date_pair_streams_its_own_fare_for_the_calendar(self):
        payload = dict(password="Victor", origin="MIL", destination="LIM", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=12, max_duration=13)

        def offer(price):
            return {"total_amount": str(price), "total_currency": "EUR", "live_mode": True,
                    "_airlines": "Example Airline", "_searched_at": "2026-10-18T00:00:00+00:00",
                    "_search_url": "https://www.google.com/travel/flights",
                    "slices": [{"segments": [{"origin": {"iata_code": "MXP"},
                                               "destination": {"iata_code": "LIM"}}], "duration": "PT720M"}]}

        with endpoint() as address, patch("api.search.GoogleFlightsClient") as Client:
            Client.return_value.search_offers.side_effect = [[offer(120)], [offer(180)]]
            status, body = post(address, payload)
        self.assertEqual(status, 200)
        events = [json.loads(line) for line in body.splitlines()]
        quoted = [e for e in events if e["type"] == "progress" and e["status"] == "ok"]
        self.assertEqual([e["offer"]["price"] for e in quoted], [120, 180])
        self.assertEqual([e["return_date"] for e in quoted],
                         [(DEPARTURE + timedelta(days=d)).isoformat() for d in (12, 13)])
        self.assertTrue(all(e["offer"]["currency"] == "EUR" for e in quoted))
        summary = next(e["row"] for e in events if e["type"] == "row")
        self.assertEqual(summary["best_price"], "120.00")
        self.assertTrue(all(e["offer"] is None for e in events
                            if e["type"] == "progress" and e["status"] == "running"))

    def test_native_city_filter_progress_and_targeted_retry(self):
        payload = dict(password="Victor", origin="MIL", destination="LON", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=12, max_duration=17, exclude_self_transfers=False)
        pair = search_queries(config())[2].payload()
        payload["retry_queries"] = [pair]
        with endpoint() as address, patch("api.search.GoogleFlightsClient") as Client:
            Client.return_value.search_offers.return_value = []
            status, body = post(address, payload)
            events = [json.loads(line) for line in body.splitlines()]
            self.assertEqual(status, 200)
            self.assertEqual(Client.call_args.kwargs["exclude_self_transfers"], False)
            self.assertEqual(Client.return_value.search_offers.call_count, 1)
            self.assertEqual(Client.return_value.search_offers.call_args.kwargs["origin"], "MIL")
            self.assertEqual(events[0]["queries"], [pair])
            self.assertTrue(events[0]["is_retry"])
            self.assertEqual([e["type"] for e in events], ["start", "progress", "progress", "row", "done"])
            self.assertEqual(events[-1]["completed"], 1)
            self.assertEqual(events[-1]["failed_queries"], [])
            Client.reset_mock()
            payload["retry_queries"] = [pair, pair]
            status, _ = post(address, payload)
            self.assertEqual(status, 400)
            Client.assert_not_called()
