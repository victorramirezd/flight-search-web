import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fast_flights.exceptions import FlightsNotFound
from flight_search import ApiError, ProviderUnavailableError
from google_flights import ConsentAwareFetcher, GoogleFlightsClient, REQUEST_COUNTERS

CONSENT = '<form action="https://consent.google.com/save"><input name="set_eom" value="true"></form>'
RESULTS = '<script class="ds:1">data:[],</script>'


def response(body):
    return SimpleNamespace(status_code=200, text=body)


def network():
    client = Mock()
    client.get.side_effect = [response(CONSENT), response(RESULTS), response(RESULTS), response(RESULTS)]
    client.post.return_value = response('saved')
    return client


def search(client, day):
    return client.search_offers(origin='LIM', destination='MIL', departure_date=date(2026, 12, day),
                                return_date=date(2026, 12, 28), adults=1, max_results=20)


def fetch(query, *, integration):
    integration.fetch_html(query)
    return []


class SessionsTest(unittest.TestCase):
    def test_invalid_mode_does_not_create_a_network_session(self):
        with patch('google_flights.Client') as constructor:
            for value in (None, True, [], {}, 1, 'Fresh', '', 'rotate'):
                with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'session_mode'):
                    GoogleFlightsClient(session_mode=value)
            constructor.assert_not_called()

    @patch('google_flights.get_flights', side_effect=fetch)
    @patch('google_flights.time.sleep')
    def test_shared_and_fresh_cookie_jars_and_actual_consent_request_counts(self, sleep, flights):
        for mode, expected_clients, expected_counts in [
                ('shared', 1, [(3, 1, 1), (1, 0, 0)]),
                ('fresh', 2, [(3, 1, 1), (3, 1, 1)])]:
            with self.subTest(mode=mode), patch('google_flights.Client', side_effect=lambda **kw: network()) as clients:
                client = GoogleFlightsClient(session_mode=mode)
                integrations = []
                for day, (http, posts, refetches) in zip((10, 11), expected_counts):
                    search(client, day)
                    integrations.append(client.integration)
                    diag = client.last_query_diagnostics
                    self.assertEqual(diag['session_mode'], mode)
                    self.assertEqual(diag['http_requests'], http)
                    self.assertEqual(diag['consent_post_requests'], posts)
                    self.assertEqual(diag['consent_refetch_requests'], refetches)
                    self.assertEqual(diag['flight_get_requests'], http - posts)
                self.assertEqual(clients.call_count, expected_clients)
                self.assertEqual(client.sessions_created, expected_clients)
                self.assertEqual(client.last_query_diagnostics['session_index'], expected_clients)
                self.assertEqual(integrations[0] is integrations[1], mode == 'shared')
                if mode == 'fresh':
                    self.assertIsNot(integrations[0].client, integrations[1].client)

    @patch('google_flights.get_flights', side_effect=fetch)
    def test_fresh_sessions_do_not_reset_pacing(self, flights):
        with patch('google_flights.Client', side_effect=lambda **kw: network()), \
                patch('google_flights.time.monotonic', side_effect=[100, 100.25, 101.5]), \
                patch('google_flights.time.sleep') as sleep:
            client = GoogleFlightsClient(session_mode='fresh')
            search(client, 10)
            search(client, 11)
            sleep.assert_called_once_with(1.25)
            self.assertEqual(client.last_query_diagnostics['start_gap_ms'], 1500)

    def test_fresh_sessions_do_not_reset_consecutive_failure_stop(self):
        def fails(query, *, integration):
            integration.fetch_html(query)
            raise FlightsNotFound('received error')
        with patch('google_flights.Client', side_effect=lambda **kw: network()), \
                patch('google_flights.time.sleep'), patch('google_flights.get_flights', side_effect=fails):
            client = GoogleFlightsClient(session_mode='fresh')
            for day in (10, 11):
                with self.assertRaises(ApiError) as error:
                    search(client, day)
                self.assertNotIsInstance(error.exception, ProviderUnavailableError)
            with self.assertRaisesRegex(ProviderUnavailableError, '3 consecutive') as error:
                search(client, 12)
            self.assertEqual(error.exception.diagnostics['session_index'], 3)
            self.assertEqual(error.exception.diagnostics['consent_post_requests'], 1)

    def test_request_attempt_counts_survive_consent_post_and_refetch_failures(self):
        for stage, counts in [('post', (2, 1, 0)), ('refetch', (3, 1, 1))]:
            with self.subTest(stage=stage), patch('google_flights.Client', return_value=network()):
                fetcher = ConsentAwareFetcher()
                if stage == 'post':
                    fetcher.client.post.side_effect = OSError('private')
                else:
                    fetcher.client.get.side_effect = [response(CONSENT), OSError('private')]
                with self.assertRaises(RuntimeError):
                    fetcher.fetch_html('query')
                self.assertEqual(tuple(fetcher.diagnostics[k] for k in
                                       ('http_requests', 'consent_post_requests', 'consent_refetch_requests')), counts)
                self.assertEqual(fetcher.diagnostics['category'], 'consent_error' if stage == 'post' else 'network_error')
