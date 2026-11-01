"""Controlled fetch-path comparison without arbitrary URL access or changed defaults."""
from datetime import date, timedelta
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google_flights import ConsentAwareFetcher, GoogleFlightsClient
from test_search_queries import endpoint, post


class FetchPathTest(unittest.TestCase):
    def test_both_paths_keep_consent_refetch_on_the_selected_url(self):
        consent = '<form action="https://consent.google.com/save"><input name="set_eom" value="true"></form>'
        result = '<script class="ds:1">data:[],</script>'
        for mode, suffix in [('root', ''), ('search', '/search')]:
            fetcher = ConsentAwareFetcher(fetch_path=mode)
            fetcher.client = Mock()
            fetcher.client.get.side_effect = [SimpleNamespace(status_code=200, text=t)
                                              for t in [consent, result]]
            fetcher.client.post.return_value = SimpleNamespace(status_code=200)
            query = Mock(); query.params.return_value = {'tfs': 'same-query'}
            self.assertEqual(fetcher.fetch_html(query), result)
            calls = fetcher.client.get.call_args_list
            self.assertEqual(calls[0], calls[1])
            self.assertEqual(calls[0].args[0], 'https://www.google.com/travel/flights' + suffix)
            self.assertEqual(fetcher.diagnostics['http_requests'], 3)

    @patch('google_flights.time.sleep')
    @patch('google_flights.get_flights', return_value=[])
    def test_fresh_sessions_keep_selected_path_and_report_it(self, fetch, sleep):
        client = GoogleFlightsClient(fetch_path='search', session_mode='fresh')
        for offset in [0, 1]:
            client.search_offers(origin='MIL', destination='IST_ALL',
                                 departure_date=date.today()+timedelta(days=30+offset),
                                 return_date=None, adults=1, max_results=20)
            self.assertEqual(fetch.call_args.kwargs['integration'].fetch_path, 'search')
            self.assertEqual(client.last_query_diagnostics['fetch_path'], 'search')
        self.assertEqual(client.sessions_created, 2)
        self.assertEqual(GoogleFlightsClient().fetch_path, 'root')

    def test_endpoint_validates_before_stream_and_reports_selected_path(self):
        payload = dict(password='victor', origin='MIL', destination='IST_ALL',
                       target_date=(date.today()+timedelta(days=30)).isoformat(),
                       date_flex_days=0, min_duration=3, max_duration=3)
        with endpoint() as address, patch('api.search.GoogleFlightsClient') as Client, \
                patch('api.search.sys.stderr', new_callable=io.StringIO):
            Client.return_value.search_offers.return_value = []
            for mode in ['root', 'search']:
                status, body = post(address, dict(payload, fetch_path=mode))
                self.assertEqual(status, 200)
                events = [json.loads(line) for line in body.splitlines()]
                self.assertEqual(events[0]['fetch_path'], mode)
                self.assertEqual(events[-1]['fetch_path'], mode)
                self.assertEqual(Client.call_args.kwargs['fetch_path'], mode)
            Client.reset_mock()
            for mode in [None, True, {}, [], 'https://example.com', '../', 'SEARCH']:
                status, body = post(address, dict(payload, fetch_path=mode))
                self.assertEqual(status, 400)
                self.assertIn('fetch_path', json.loads(body)['error'])
            Client.assert_not_called()
