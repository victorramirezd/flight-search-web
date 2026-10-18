"""Exercise Duffel transport through the streaming endpoint without paid requests."""
import copy
import json
import unittest
from datetime import date, timedelta
from unittest.mock import Mock, patch

import requests
from flight_search import (DuffelClient, ProviderUnavailableError, RateLimitError,
                           best_offer_summary)
from test_search_queries import endpoint, post

DEPARTURE = date.today() + timedelta(days=30)


def offer(price=100, currency='GBP', connections=0):
    def segment(origin, destination, departing, arriving):
        return {'origin': {'iata_code': origin}, 'destination': {'iata_code': destination},
                'departing_at': departing, 'arriving_at': arriving,
                'operating_carrier': {'name': 'British Airways', 'iata_code': 'BA'},
                'marketing_carrier': {'name': 'British Airways', 'iata_code': 'BA'},
                'marketing_carrier_flight_number': '123', 'duration': 'PT2H'}
    outbound = [segment('MXP', 'LHR', '2027-02-04T09:00:00', '2027-02-04T10:00:00')]
    if connections:
        outbound.append(segment('LHR', 'LIM', '2027-02-04T12:00:00', '2027-02-04T20:00:00'))
    inbound = [segment('LHR', 'MXP', '2027-02-17T11:00:00', '2027-02-17T14:00:00')]
    if connections:
        inbound.append(segment('MXP', 'FCO', '2027-02-17T16:00:00', '2027-02-17T18:00:00'))
    return {'id': f'off_{price}', 'total_amount': str(price), 'total_currency': currency,
            'slices': [{'duration': 'PT2H', 'segments': outbound},
                       {'duration': 'PT2H', 'segments': inbound}]}


def response(offers=(), status=201):
    result = Mock(status_code=status, headers={})
    result.json.return_value = {'data': {'id': 'orq_fixture', 'live_mode': False,
                                       'offers': copy.deepcopy(list(offers))},
                               'errors': [{'title': 'Fixture failure'}]}
    return result


class DuffelSearchTest(unittest.TestCase):
    def client(self, *responses, **kwargs):
        session = Mock()
        session.post.side_effect = responses
        client = DuffelClient('duffel_test_fixture', session, **kwargs)
        client.wait_for_request_slot = Mock()
        return client

    def search(self, client, **changes):
        args = dict(origin='MIL', destination='LON', departure_date=DEPARTURE,
                    return_date=DEPARTURE + timedelta(days=13), adults=2, max_results=20)
        return client.search_offers(**(args | changes))

    def test_city_payload_currency_and_complete_itinerary_metadata(self):
        client = self.client(response([offer()]))
        found = self.search(client)
        sent = client.session.post.call_args.kwargs
        self.assertEqual(sent['json']['data']['slices'][0]['origin'], 'MIL')
        self.assertEqual(sent['json']['data']['slices'][1]['origin'], 'LON')
        self.assertEqual(sent['json']['data']['max_connections'], 2)
        self.assertEqual(len(sent['json']['data']['passengers']), 2)
        self.assertEqual(sent['headers']['Authorization'], 'Bearer duffel_test_fixture')
        summary = best_offer_summary(found)
        self.assertEqual(summary.currency, 'GBP')
        self.assertEqual(summary.mode, 'test')
        self.assertEqual(summary.provider, 'duffel')
        self.assertEqual(summary.airports, 'MXP -> LHR / LHR -> MXP')
        self.assertIn('2027-02-17 11:00:00', summary.flight_segments)
        self.assertIn('BA123', summary.flight_segments)
        self.assertEqual(summary.total_duration, '4h')
        self.assertTrue(summary.searched_at)
        self.assertEqual(summary.airline_url, 'https://www.britishairways.com/')

    def test_filter_before_offer_limit_and_invalid_prices(self):
        client = self.client(response([offer(10), offer(20, connections=1),
                                       offer('NaN'), offer('Infinity'), offer(-1)]),
                             min_connections=1)
        found = self.search(client, max_results=1)
        self.assertEqual([o['total_amount'] for o in found], ['20'])
        invalid = self.client(response([offer('NaN'), offer('Infinity'), offer(-1), offer(0)]))
        self.assertEqual([o['total_amount'] for o in self.search(invalid)], ['0'])

    def test_network_malformed_auth_and_rate_limit_stop_the_pass(self):
        for result in (requests.ConnectionError('Fixture offline'),
                       Mock(status_code=201, json=Mock(return_value={'data': {}})),
                       response(status=401), response(status=503)):
            with self.subTest(result=result), self.assertRaises(ProviderUnavailableError):
                self.search(self.client(result), retries=0)
        client = self.client(response(status=429))
        with self.assertRaises(RateLimitError):
            self.search(client, retries=0)

    def test_bounded_rate_limit_retry_keeps_pacing(self):
        client = self.client(response(status=429), response([offer()]))
        with patch('flight_search.time.sleep') as sleep:
            self.assertEqual(len(self.search(client, retries=1)), 1)
        self.assertEqual(client.session.post.call_count, 2)
        self.assertEqual(client.wait_for_request_slot.call_count, 2)
        sleep.assert_called_once_with(2)

    def test_currency_mismatch_never_compares_prices(self):
        client = self.client(response([offer(currency='EUR')]))
        client.currency = 'GBP'
        with self.assertRaisesRegex(ProviderUnavailableError, 'mixed currencies'):
            self.search(client)

    def test_real_client_streams_pair_prices_partial_fares_and_targeted_recovery(self):
        payload = dict(password='victor', origin='MIL', destination='LON',
                       target_date=DEPARTURE.isoformat(), date_flex_days=0,
                       min_duration=12, max_duration=14)
        failed = {'departure_date': DEPARTURE.isoformat(),
                  'return_date': (DEPARTURE + timedelta(days=13)).isoformat()}
        with endpoint() as address, patch.dict('os.environ', {'DUFFEL_ACCESS_TOKEN': 'duffel_test_fixture'}), \
                patch('flight_search.DuffelClient.wait_for_request_slot'), \
                patch('flight_search.requests.Session') as Session:
            Session.return_value.post.side_effect = [response([offer(120)]),
                                                     requests.ConnectionError('Fixture offline')]
            status, body = post(address, payload)
            events = [json.loads(line) for line in body.splitlines()]
            self.assertEqual(status, 200)
            self.assertTrue(events[0]['mode_warning'])
            self.assertEqual(events[0]['provider'], 'duffel')
            self.assertEqual(events[-1]['type'], 'error')
            self.assertEqual(events[-1]['completed'], 2)
            self.assertEqual(len(events[-1]['pending_queries']), 1)
            row = next(e['row'] for e in events if e['type'] == 'row')
            self.assertEqual(row['best_price'], '120.00')
            self.assertEqual(row['search_status'], 'partial')
            quoted = next(e['offer'] for e in events if e['type'] == 'progress' and e['status'] == 'ok')
            self.assertEqual(quoted['currency'], 'GBP')
            Session.return_value.post.reset_mock(side_effect=True)
            Session.return_value.post.return_value = response([offer(100)])
            status, body = post(address, payload | {'retry_queries': [failed], 'currency': 'GBP'})
            events = [json.loads(line) for line in body.splitlines()]
            self.assertEqual(events[-1]['completed'], 1)
            self.assertEqual(events[-1]['failed_queries'], [])
            self.assertEqual(Session.return_value.post.call_count, 1)
            self.assertEqual(next(e['row'] for e in events if e['type'] == 'row')['best_price'], '100.00')

    def test_missing_token_and_invalid_retry_currency_are_json_errors(self):
        payload = dict(password='victor', origin='MIL', destination='LON',
                       target_date=DEPARTURE.isoformat(), date_flex_days=0, one_way=True)
        with endpoint() as address, patch.dict('os.environ', {}, clear=True):
            status, body = post(address, payload)
            self.assertEqual(status, 500)
            self.assertIn('DUFFEL_ACCESS_TOKEN', json.loads(body)['error'])
            status, _ = post(address, payload | {'currency': 'bad currency'})
            self.assertEqual(status, 400)
