import json
import subprocess
import sys
import threading
import unittest
from contextlib import contextmanager
from datetime import date, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

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
        code = '''import runpy, sys, os
os.environ['DUFFEL_ACCESS_TOKEN'] = 'duffel_test_fixture'
from unittest.mock import Mock
import requests
requests.Session = Mock(return_value=Mock(post=Mock(side_effect=requests.ConnectionError('Duffel returned HTTP 429'))))
from datetime import date, timedelta
sys.argv = ['flight_search.py', '--origin', 'MIL', '--destination', 'LON',
            '--target-date', (date.today()+timedelta(days=30)).isoformat(),
            '--date-flex-days', '0', '--one-way']
runpy.run_module('flight_search', run_name='__main__', alter_sys=True)
'''
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(result.returncode, 4)
        self.assertIn("Search error: Network error while contacting Duffel: Duffel returned HTTP 429", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_city_config_defaults_and_ordered_grid(self):
        cfg = config(date_flex_days=4)
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
    def setUp(self):
        self.env = patch.dict("os.environ", {"DUFFEL_ACCESS_TOKEN": "duffel_test_fixture"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_each_date_pair_streams_its_own_fare_for_the_calendar(self):
        payload = dict(password="Victor", origin="MIL", destination="LIM", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=12, max_duration=13)

        def offer(price):
            return {"total_amount": str(price), "total_currency": "EUR", "live_mode": True,
                    "_airlines": "Example Airline", "_searched_at": "2026-10-18T00:00:00+00:00",
                    "_search_url": "https://www.latamairlines.com/",
                    "slices": [{"segments": [{"origin": {"iata_code": "MXP"},
                                               "destination": {"iata_code": "LIM"}}], "duration": "PT720M"}]}

        with endpoint() as address, patch("api.search.DuffelClient") as Client:
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

    def test_city_connections_progress_and_targeted_retry(self):
        payload = dict(password="Victor", origin="MIL", destination="LON", target_date=DEPARTURE.isoformat(),
                       date_flex_days=0, min_duration=12, max_duration=17, max_connections_per_slice=2)
        pair = search_queries(config())[2].payload()
        payload["retry_queries"] = [pair]
        with endpoint() as address, patch("api.search.DuffelClient") as Client:
            Client.return_value.search_offers.return_value = []
            status, body = post(address, payload)
            events = [json.loads(line) for line in body.splitlines()]
            self.assertEqual(status, 200)
            self.assertEqual(Client.call_args.kwargs["max_connections"], 2)
            self.assertEqual(Client.call_args.args[0], "duffel_test_fixture")
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
