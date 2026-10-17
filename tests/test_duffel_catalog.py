import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts.validate_duffel_catalog import (
    CatalogError, compare_catalog, fetch_list, write_reports,
)


def airport(code='PMO', **changes):
    return dict(id='arp_' + code.lower() + '_it', iata_code=code, name='Palermo Airport',
                city_name='Palermo', iata_country_code='IT', latitude=38.176,
                longitude=13.091, **changes)


def local(code='PMO', **changes):
    value = dict(code=code, name='Falcone–Borsellino Airport', city='Palermo', country='IT',
                 type='large_airport', latitude=38.175999, longitude=13.091)
    return {**value, **changes}


def response(data, after=None, status=200):
    result = Mock(status_code=status, headers={})
    result.json.return_value = dict(data=data, meta=dict(after=after))
    return result


class DuffelCatalogTest(unittest.TestCase):
    def test_all_pages_are_read_until_null_cursor(self):
        session = Mock()
        session.get.side_effect = [response([airport()], 'next'), response([airport('FCO')])]
        records, pages = fetch_list(session, 'airport', sleep=Mock(), progress=Mock())
        self.assertEqual([record['iata_code'] for record in records], ['PMO', 'FCO'])
        self.assertEqual(pages, 2)
        self.assertEqual(session.get.call_args.kwargs['params'], {'limit': 200, 'after': 'next'})
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_incomplete_or_repeating_pagination_is_rejected(self):
        cases = [
            [response([airport()], 'next'), response([airport('FCO')], 'next')],
            [response([airport()], 'next'), response([airport()])],
        ]
        missing = response([airport()])
        missing.json.return_value = dict(data=[airport()])
        cases.append([missing])
        for pages in cases:
            session = Mock()
            session.get.side_effect = pages
            with self.subTest(pages=len(pages)), self.assertRaises(CatalogError):
                fetch_list(session, 'airport', sleep=Mock(), progress=Mock())

    def test_rate_limit_is_retried_without_skipping_page(self):
        session, sleep = Mock(), Mock()
        session.get.side_effect = [response([], status=429), response([airport()])]
        records, pages = fetch_list(session, 'airport', sleep=sleep, progress=Mock())
        self.assertEqual(len(records), 1)
        self.assertEqual(pages, 1)
        self.assertEqual(session.get.call_args_list[0], session.get.call_args_list[1])
        sleep.assert_called_once()

    def test_identity_not_name_decides_confirmation(self):
        rows, proposed = compare_catalog([local()], [airport()], [])
        self.assertEqual(rows[0]['status'], 'confirmed')
        self.assertEqual(proposed[0]['name'], 'Palermo Airport')
        self.assertEqual(proposed[0]['duffel_id'], 'arp_pmo_it')
        self.assertIn('Falcone–Borsellino Airport', proposed[0]['aliases'])
        self.assertEqual(proposed[0]['code'], 'PMO')

    def test_name_match_never_substitutes_a_different_code(self):
        rows, proposed = compare_catalog([local('PAL')], [airport()], [])
        self.assertEqual(rows[0]['status'], 'not_in_duffel')
        self.assertEqual(proposed, [])

    def test_country_and_type_mismatches_are_quarantined(self):
        rows, proposed = compare_catalog([local(country='US', latitude=0, longitude=0)], [airport()], [])
        self.assertEqual(rows[0]['status'], 'country_mismatch')
        self.assertEqual(proposed, [])
        rows, proposed = compare_catalog([local(type='city')], [airport()], [])
        self.assertEqual(rows[0]['status'], 'type_mismatch')
        self.assertEqual(proposed, [])

    def test_duplicate_codes_are_not_silently_overwritten(self):
        rows, proposed = compare_catalog([local(), local()], [airport()], [])
        self.assertEqual([row['status'] for row in rows], ['duplicate_local_code'] * 2)
        self.assertEqual(proposed, [])

    def test_city_membership_comes_from_duffel(self):
        city = dict(id='cit_mil_it', iata_code='MIL', iata_country_code='IT', name='Milan',
                    airports=[airport('MXP'), airport('LIN')])
        rows, proposed = compare_catalog([local('MIL', type='city', name='Milan', city='Milan', country='Italy')], [], [city])
        self.assertEqual(rows[0]['status'], 'confirmed')
        self.assertEqual(proposed[0]['airport_codes'], ['LIN', 'MXP'])
        self.assertEqual(proposed[0]['country'], 'IT')

    def test_distant_old_names_do_not_become_aliases(self):
        rows, proposed = compare_catalog([local(latitude=0, longitude=0)], [airport()], [])
        self.assertGreater(rows[0]['coordinate_distance_km'], 100)
        self.assertNotIn('aliases', proposed[0])
        self.assertEqual(proposed[0]['latitude'], 0)
        self.assertEqual(proposed[0]['name'], 'Falcone–Borsellino Airport')
        self.assertIn('provider_coordinates_differ', proposed[0]['validation_warnings'])

    def test_matching_geometry_preserves_country_while_recording_provider_conflict(self):
        rows, proposed = compare_catalog([local(country='US')], [airport()], [])
        self.assertEqual(rows[0]['status'], 'confirmed_country_exception')
        self.assertEqual(proposed[0]['country'], 'US')
        self.assertEqual(proposed[0]['duffel_country'], 'IT')

    def test_embedded_airport_is_validated_when_absent_from_airport_list(self):
        city = dict(id='cit_pmo_it', iata_code='PMO', iata_country_code='IT',
                    name='Palermo', airports=[airport()])
        rows, proposed = compare_catalog([local()], [], [city])
        self.assertEqual(rows[0]['status'], 'confirmed')
        self.assertEqual(proposed[0]['type'], 'large_airport')

    def test_primary_airport_wins_over_a_duplicate_membership_alias_id(self):
        member = {**airport(), 'id': 'old-alias-id'}
        city = dict(id='cit_pmo_it', iata_code='PMO', iata_country_code='IT',
                    name='Palermo', airports=[member])
        rows, proposed = compare_catalog([local()], [airport()], [city])
        self.assertEqual(rows[0]['status'], 'confirmed')
        self.assertEqual(proposed[0]['duffel_id'], airport()['id'])

    def test_airport_code_that_duffel_defines_only_as_city_is_relabelled(self):
        city = dict(id='cit_pbi_us', iata_code='PBI', iata_country_code='US',
                    name='West Palm Beach', airports=[])
        rows, proposed = compare_catalog([local('PBI', country='US')], [], [city])
        self.assertEqual(rows[0]['status'], 'converted_to_city')
        self.assertEqual(proposed[0]['type'], 'city')
        self.assertEqual(proposed[0]['name'], 'West Palm Beach')
        self.assertNotIn('latitude', proposed[0])
        self.assertNotIn('aliases', proposed[0])

    def test_reports_cover_every_local_entry(self):
        places = [local(), local('ZZZ')]
        rows, proposed = compare_catalog(places, [airport()], [])
        snapshot = dict(retrieved_at='2026-10-17T00:00:00Z', sources=['Duffel'], airports=[airport()], cities=[])
        with tempfile.TemporaryDirectory() as directory:
            summary = write_reports(Path(directory), places, snapshot, rows, proposed)
            self.assertEqual(summary['status_counts'], {'confirmed': 1, 'not_in_duffel': 1})
            self.assertEqual(len(json.loads((Path(directory) / 'comparison.json').read_text())), 2)

    def test_failed_download_does_not_write_report_or_catalog(self):
        from scripts.validate_duffel_catalog import main
        with tempfile.TemporaryDirectory() as directory:
            catalog = Path(directory) / 'airports.json'
            original = json.dumps([local()])
            catalog.write_text(original)
            output = Path(directory) / 'report'
            with patch('sys.argv', ['validator', '--catalog', str(catalog), '--output', str(output), '--write-catalog']), patch.dict(
                'os.environ', {'DUFFEL_ACCESS_TOKEN': 'not-a-real-token'}
            ), patch('scripts.validate_duffel_catalog.fetch_list', side_effect=CatalogError('incomplete download')):
                self.assertEqual(main(), 1)
            self.assertFalse(output.exists())
            self.assertEqual(catalog.read_text(), original)


if __name__ == '__main__':
    unittest.main()
