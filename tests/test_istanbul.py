import copy
from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from fast_flights import create_query, FlightQuery, Passengers
from flight_search import build_search_config, location_airport_codes
from google_flights import GoogleFlightsClient, recognized_empty_results
from google_query import with_browser_encoding
from test_google_flights import itinerary, leg
from test_search_queries import endpoint, post

FIXTURE = json.loads((Path(__file__).parent/'fixtures/google-empty-missing-airlines.json').read_text())['payload']


def script(payload):
    return 'data:'+json.dumps(payload)+','


class IstanbulTest(unittest.TestCase):
    def test_precise_empty_response_detection_does_not_mask_malformed_or_priced_data(self):
        self.assertTrue(recognized_empty_results(script(FIXTURE)))
        for bad in (None, [], {}, FIXTURE[:4]):
            self.assertFalse(recognized_empty_results(script(bad)))
        for content in ('invalid', 'data:errorHasStatus: true,'):
            self.assertFalse(recognized_empty_results(content))
        priced=copy.deepcopy(FIXTURE);priced[2]=[[['priced entry']]]
        self.assertFalse(recognized_empty_results(script(priced)))
        missing=copy.deepcopy(FIXTURE);missing[7]=[]
        self.assertFalse(recognized_empty_results(script(missing)))

    def test_omitted_optional_airlines_yields_unpriced_without_upstream_parser_crash(self):
        network=Mock();network.get.return_value=SimpleNamespace(status_code=200,text='<script class="ds:1">'+script(FIXTURE)+'</script>')
        with patch('google_flights.Client',return_value=network):
            client=GoogleFlightsClient();client.consecutive_query_errors=2
            offers=client.search_offers(origin='MIL',destination='ISL',departure_date=date(2026,12,13),
                                        return_date=date(2026,12,16),adults=1,max_results=20)
        self.assertEqual(offers,[])
        self.assertEqual(client.last_query_diagnostics['category'],'no_matching_priced_results')
        self.assertEqual(client.last_query_diagnostics['response_kind'],'empty_results')
        self.assertEqual(client.last_query_diagnostics['http_requests'],1)
        self.assertEqual(client.consecutive_query_errors,0)

    def test_metropolitan_selector_preserves_individual_ist_code(self):
        self.assertEqual(location_airport_codes('IST_ALL'),{'IST','SAW'})
        self.assertEqual(location_airport_codes('IST'),{'IST'})
        self.assertEqual(location_airport_codes('SAW'),{'SAW'})
        for destination,expected,kind in [('IST_ALL','/m/09949m',3),('IST','IST',1),('SAW','SAW',1)]:
            query=create_query(flights=[FlightQuery(date='2026-12-13',from_airport='MIL',to_airport=destination)],
                               passengers=Passengers(adults=1),seat='economy',trip='one-way',currency='EUR',language='en')
            location=with_browser_encoding(query).pb().data[0].to_airport
            self.assertEqual((location.airport,location.entity_type),(expected,kind))
        with self.assertRaises(ValueError):
            build_search_config(origin='MIL',destination='XYZ_ALL',target_date='2026-12-13',date_flex_days=0)

    @patch('google_flights.get_flights')
    def test_actual_endpoints_are_accepted_only_for_the_selected_airport_group(self, fetch):
        fetch.return_value=[itinerary(100,[leg('MXP','IST')]),itinerary(90,[leg('BGY','SAW')]),itinerary(80,[leg('MXP','ISL')])]
        departure=date.today()+timedelta(days=30)
        for destination,expected in [('IST_ALL',[90,100]),('IST',[100]),('SAW',[90])]:
            offers=GoogleFlightsClient().search_offers(origin='MIL',destination=destination,departure_date=departure,
                                                       return_date=None,adults=1,max_results=20)
            self.assertEqual([float(o['total_amount']) for o in offers],expected)

    def test_endpoint_rejects_inactive_airport_before_stream_and_accepts_city_selector(self):
        payload=dict(password='victor',origin='MIL',destination='ISL',target_date='2026-12-13',
                     date_flex_days=0,min_duration=3,max_duration=3)
        with endpoint() as address,patch('api.search.GoogleFlightsClient') as Client:
            status,body=post(address,payload)
            self.assertEqual(status,400)
            self.assertIn('does not serve scheduled passenger',json.loads(body)['error'])
            self.assertIn('SAW',json.loads(body)['error'])
            Client.assert_not_called()
            Client.return_value.search_offers.return_value=[]
            status,body=post(address,dict(payload,destination='IST_ALL'))
            self.assertEqual(status,200)
            self.assertEqual(Client.return_value.search_offers.call_args.kwargs['destination'],'IST_ALL')
            self.assertEqual(json.loads(body.splitlines()[-1])['type'],'done')
