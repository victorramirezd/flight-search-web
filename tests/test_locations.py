import unittest
from datetime import date, timedelta
from flight_search import ApiError, build_search_config, iter_search_rows

DEPARTURE = date.today() + timedelta(days=30)


class LocationsTest(unittest.TestCase):
    def config(self, **changes):
        return build_search_config(**dict(origin='PMO', destination='FCO',
            target_date=DEPARTURE.isoformat(), date_flex_days=0, one_way=True, **changes))

    def test_rejects_names_instead_of_slicing_them(self):
        for value in ['Palermo', '', 'PMO garbage', 'ZZZ']:
            with self.assertRaises(ValueError):
                build_search_config(origin=value, destination='FCO', target_date=DEPARTURE.isoformat())

    def test_iata_codes_reach_provider_unchanged(self):
        calls = []
        class Client:
            def search_offers(self, **kwargs):
                calls.append(kwargs)
                return []
        row = next(iter(iter_search_rows(Client(), self.config())))
        self.assertEqual(calls[0]['origin'], 'PMO')
        self.assertEqual(row['search_status'], 'no_offers')

    def test_provider_failure_is_not_no_offers(self):
        class Client:
            def search_offers(self, **kwargs):
                raise ApiError('Invalid origin code')
        row = next(iter(iter_search_rows(Client(), self.config())))
        self.assertEqual(row['search_status'], 'error')
        self.assertIn('Invalid origin code', row['search_message'])

    def test_partial_failures_are_visible(self):
        class Client:
            def search_offers(self, **kwargs):
                if kwargs['return_date'] == DEPARTURE + timedelta(days=13):
                    raise ApiError('Supplier error')
                return []
        config = build_search_config(origin='PMO', destination='FCO', target_date=DEPARTURE.isoformat(),
            date_flex_days=0, min_duration=13, max_duration=14)
        row = next(iter(iter_search_rows(Client(), config)))
        self.assertIn('1 of 2', row['search_message'])


class SearchEndpointTest(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        self.env = patch.dict("os.environ", {"DUFFEL_ACCESS_TOKEN": "duffel_test_fixture"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_browser_codes_reach_duffel_and_bad_input_is_rejected(self):
        import json
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        from api.search import handler

        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        payload = dict(password='victor', origin='PMO', destination='FCO',
                       target_date=DEPARTURE.isoformat(), date_flex_days=0, one_way=True)
        try:
            with patch.dict('os.environ', {'DUFFEL_ACCESS_TOKEN': 'duffel_test_fixture'}, clear=True), patch(
                'api.search.DuffelClient.search_offers', return_value=[]
            ) as provider:
                connection = HTTPConnection(*server.server_address)
                connection.request('POST', '/api/search', json.dumps(payload), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                events = [json.loads(line) for line in response.read().splitlines()]
                self.assertEqual(response.status, 200)
                self.assertEqual(provider.call_args.kwargs['origin'], 'PMO')
                self.assertEqual(next(e['row'] for e in events if e['type'] == 'row')['search_status'], 'no_offers')
                connection.close()
                provider.reset_mock()
                payload['origin'] = 'Palermo'
                connection = HTTPConnection(*server.server_address)
                connection.request('POST', '/api/search', json.dumps(payload), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                self.assertEqual(response.status, 400)
                self.assertIn('IATA code', json.loads(response.read())['error'])
                provider.assert_not_called()
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_provider_failure_is_streamed_once_without_done_or_empty_results(self):
        import json
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch
        from api.search import handler
        from flight_search import ProviderUnavailableError

        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        payload = dict(password='Victor', origin='FCO', destination='LHR',
                       target_date=DEPARTURE.isoformat(), date_flex_days=2, one_way=True)
        try:
            with patch('api.search.DuffelClient.search_offers',
                       side_effect=ProviderUnavailableError('Duffel returned an invalid offers response')) as provider:
                connection = HTTPConnection(*server.server_address)
                connection.request('POST', '/api/search', json.dumps(payload), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                events = [json.loads(line) for line in response.read().splitlines()]
                self.assertEqual([event['type'] for event in events], ['start', 'progress', 'progress', 'error'])
                self.assertEqual(events[-1]['status'], 502)
                self.assertEqual(events[-1]['completed'], 1)
                self.assertEqual(len(events[-1]['failed_queries']), 1)
                self.assertEqual(len(events[-1]['pending_queries']), 4)
                self.assertEqual(provider.call_count, 1)
                connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
