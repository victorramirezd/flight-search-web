from datetime import date, timedelta
import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from google_flights import ConsentAwareFetcher, GoogleFlightsClient, RequestsClient
from test_search_queries import endpoint, post


class HttpTransportTest(unittest.TestCase):
    @patch('google_flights.Session.request')
    def test_requests_timeout_and_consent_share_the_same_cookie_session(self, request):
        consent = '<form action="https://consent.google.com/save"><input name="set_eom" value="true"></form>'
        result = '<script class="ds:1">data:[],</script>'
        request.side_effect = [SimpleNamespace(status_code=200, text=consent),
                               SimpleNamespace(status_code=200), SimpleNamespace(status_code=200, text=result)]
        fetcher = ConsentAwareFetcher(http_transport='requests', fetch_path='search')
        self.assertIsInstance(fetcher.client, RequestsClient)
        query = Mock(); query.params.return_value = {'tfs': 'same-query'}
        self.assertEqual(fetcher.fetch_html(query), result)
        self.assertEqual([c.args[0] for c in request.call_args_list], ['GET', 'POST', 'GET'])
        self.assertTrue(all(c.kwargs['timeout'] == 30 for c in request.call_args_list))
        self.assertEqual(fetcher.diagnostics['http_requests'], 3)
        self.assertEqual(fetcher.diagnostics['consent_post_requests'], 1)

    @patch('google_flights.time.sleep')
    @patch('google_flights.get_flights', return_value=[])
    def test_transport_survives_fresh_session_replacement(self, fetch, sleep):
        client = GoogleFlightsClient(http_transport='requests', session_mode='fresh')
        for offset in [0, 1]:
            client.search_offers(origin='MIL', destination='IST_ALL',
                                 departure_date=date.today()+timedelta(days=30+offset),
                                 return_date=None, adults=1, max_results=20)
            self.assertIsInstance(client.integration.client, RequestsClient)
            self.assertEqual(client.last_query_diagnostics['http_transport'], 'requests')
        self.assertEqual(client.sessions_created, 2)
        self.assertEqual(GoogleFlightsClient().http_transport, 'primp')

    def test_api_transport_selection_and_validation(self):
        payload = dict(password='victor', origin='MIL', destination='IST_ALL',
                       target_date=(date.today()+timedelta(days=30)).isoformat(),
                       date_flex_days=0, min_duration=3, max_duration=3)
        with endpoint() as address, patch('api.search.GoogleFlightsClient') as Client, \
                patch('api.search.sys.stderr', new_callable=io.StringIO):
            Client.return_value.search_offers.return_value = []
            for mode in ['primp', 'requests']:
                status, body = post(address, dict(payload, http_transport=mode))
                self.assertEqual(status, 200)
                events = [json.loads(line) for line in body.splitlines()]
                self.assertEqual(events[0]['http_transport'], mode)
                self.assertEqual(events[-1]['http_transport'], mode)
                self.assertEqual(Client.call_args.kwargs['http_transport'], mode)
            Client.reset_mock()
            for mode in [None, True, {}, [], 'curl', 'Requests']:
                status, body = post(address, dict(payload, http_transport=mode))
                self.assertEqual(status, 400)
                self.assertIn('http_transport', json.loads(body)['error'])
            Client.assert_not_called()
