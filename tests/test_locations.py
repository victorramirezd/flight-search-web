import unittest
from datetime import date
from flight_search import ApiError, build_search_config, iter_search_rows


class LocationsTest(unittest.TestCase):
    def config(self, **changes):
        return build_search_config(**dict(origin='PMO', destination='FCO',
            target_date='2026-10-30', date_flex_days=0, one_way=True, **changes))

    def test_rejects_names_instead_of_slicing_them(self):
        for value in ['Palermo', '', 'PMO garbage', 'ZZZ']:
            with self.assertRaises(ValueError):
                build_search_config(origin=value, destination='FCO', target_date='2026-10-30')

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
                if kwargs['return_date'] == date(2026, 11, 12):
                    raise ApiError('Supplier error')
                return []
        config = build_search_config(origin='PMO', destination='FCO', target_date='2026-10-30',
            date_flex_days=0, min_duration=13, max_duration=14)
        row = next(iter(iter_search_rows(Client(), config)))
        self.assertIn('1 of 2', row['search_message'])


class SearchEndpointTest(unittest.TestCase):
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
                       target_date='2026-10-30', date_flex_days=0, one_way=True)
        try:
            with patch.dict('os.environ', {'DUFFEL_ACCESS_TOKEN': 'duffel_test_unused'}), patch(
                'api.search.DuffelClient.search_offers', return_value=[]
            ) as provider:
                connection = HTTPConnection(*server.server_address)
                connection.request('POST', '/api/search', json.dumps(payload), {'Content-Type': 'application/json'})
                response = connection.getresponse()
                events = [json.loads(line) for line in response.read().splitlines()]
                self.assertEqual(response.status, 200)
                self.assertEqual(provider.call_args.kwargs['origin'], 'PMO')
                self.assertEqual(events[1]['row']['search_status'], 'no_offers')
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


if __name__ == '__main__':
    unittest.main()
