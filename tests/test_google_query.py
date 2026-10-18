"""Native Google UI captures, submitted 2026-10-18 with matching filters."""
import base64
import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import patch

from fast_flights import FlightQuery, Passengers, create_query
from fast_flights.pb.flights_pb2 import Info
from google_query import BrowserInfo, BrowserQuery, city_entities, with_browser_encoding
from google_flights import GoogleFlightsClient
from google.protobuf.json_format import MessageToDict
from datetime import date

GOLDENS = {
    'MIL': 'CBwQAholEgoyMDI2LTEyLTEyKAJqBwgBEgNMSU1yDAgDEggvbS8wOTQ3bBolEgoyMDI2LTEyLTI4KAJqDAgDEggvbS8wOTQ3bHIHCAESA0xJTUABSAFwAYIBCwj___________8BiAEBmAEB',
    'MXP': 'CBwQAhogEgoyMDI2LTEyLTEyKAJqBwgBEgNMSU1yBwgBEgNNWFAaIBIKMjAyNi0xMi0yOCgCagcIARIDTVhQcgcIARIDTElNQAFIAXABggELCP___________wGIAQGYAQE',
}


def query(destination='MIL', **changes):
    args = dict(flights=[FlightQuery(date='2026-12-12', from_airport='LIM', to_airport=destination),
                         FlightQuery(date='2026-12-28', from_airport=destination, to_airport='LIM')],
                passengers=Passengers(adults=1), seat='economy', trip='round-trip',
                currency='EUR', language='en', max_stops=2, hide_separate_and_self_transfer=True)
    args.update(changes)
    return create_query(**args)


class BrowserQueryTest(unittest.TestCase):
    def test_matches_submitted_native_browser_city_and_airport_urls_exactly(self):
        for destination, golden in GOLDENS.items():
            with self.subTest(destination=destination):
                encoded = with_browser_encoding(query(destination))
                self.assertEqual(encoded.to_str(), golden)
                params = parse_qs(urlparse(encoded.url()).query)
                self.assertEqual(params, {key: [value] for key, value in encoded.params().items()})
                self.assertEqual(params['tfu'], ['EgYIABAAGAA'])
                self.assertNotRegex(encoded.to_str(), r'[+/=]')
                decoded = BrowserInfo.FromString(base64.urlsafe_b64decode(golden + '=' * (-len(golden) % 4)))
                self.assertEqual(decoded.all_results.value, -1)
                self.assertEqual(decoded.results_mode, 2)

    def test_keeps_original_query_and_upstream_schema_unchanged(self):
        original = query()
        before = original.to_bytes()
        encoded = with_browser_encoding(original)
        self.assertIsInstance(encoded, BrowserQuery)
        self.assertEqual(encoded.pb().data[0].to_airport.airport, '/m/0947l')
        self.assertEqual(encoded.pb().data[0].to_airport.entity_type, 3)
        self.assertEqual(encoded.pb().data[0].from_airport.entity_type, 1)
        self.assertEqual(original.to_bytes(), before)
        self.assertEqual(original.flight_data[0].to_airport.airport, 'MIL')
        self.assertNotIn('browser_source', Info.DESCRIPTOR.fields_by_name)

    def test_preserves_filters_passengers_baggage_and_other_supported_fields(self):
        for excluded in (True, False):
            original = query('MXP', passengers=Passengers(adults=2), max_stops=0,
                             hide_separate_and_self_transfer=excluded,
                             max_price=1234, carry_on_bags=1, checked_bags=1,
                             exclude_basic_economy=True, language='it', currency='USD')
            encoded = with_browser_encoding(original)
            restored = Info.FromString(encoded.to_bytes())
            self.assertEqual(MessageToDict(restored), MessageToDict(original.pb()))
            self.assertEqual(encoded.params()['hl'], 'it')
            self.assertEqual(encoded.params()['curr'], 'USD')
            self.assertEqual(restored.hide_separate_and_self_transfer, excluded)

    def test_one_way_and_unmapped_city_preserve_native_query(self):
        original = query('TYO', flights=[FlightQuery(date='2026-12-12', from_airport='LIM', to_airport='TYO')],
                         trip='one-way', max_stops=None)
        encoded = with_browser_encoding(original).pb()
        self.assertEqual(len(encoded.data), 1)
        self.assertEqual(encoded.data[0].to_airport.airport, 'TYO')
        self.assertEqual(encoded.data[0].to_airport.entity_type, 1)
        self.assertEqual(MessageToDict(Info.FromString(encoded.SerializeToString())), MessageToDict(original.pb()))

    def test_verified_city_mapping_has_valid_entities(self):
        for code, entity in city_entities().items():
            self.assertRegex(code, '^[A-Z]{3}$')
            self.assertRegex(entity['id'], '^/m/[a-zA-Z0-9_]+$')
            self.assertEqual(entity['type'], 3)

    @patch('google_flights.get_flights', return_value=[])
    def test_client_defaults_to_browser_with_explicit_legacy_baseline(self, fetch):
        for mode in ('browser', 'legacy'):
            client = GoogleFlightsClient(query_encoding=mode)
            client.search_offers(origin='LIM', destination='MIL', departure_date=date(2026,12,12),
                                 return_date=date(2026,12,28), adults=1, max_results=20)
            self.assertEqual(isinstance(fetch.call_args.args[0], BrowserQuery), mode == 'browser')
            self.assertEqual(client.last_search_url, fetch.call_args.args[0].url())
