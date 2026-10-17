import csv
import json
import re
import unittest
from pathlib import Path

from scripts.validate_duffel_catalog import digest

ROOT = Path(__file__).resolve().parents[1]
ACCEPTED = {'confirmed', 'confirmed_country_exception', 'converted_to_city'}


class CatalogIntegrityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = json.loads((ROOT / 'data/airports.json').read_text())
        cls.summary = json.loads((ROOT / 'data/airport-validation.json').read_text())
        with (ROOT / 'data/airport-validation.csv').open(newline='') as source:
            cls.audit = list(csv.DictReader(source))

    def test_catalog_matches_reviewed_evidence(self):
        self.assertEqual(digest(self.catalog), self.summary['proposed_sha256'])
        self.assertEqual(len(self.catalog), self.summary['retained_count'])
        self.assertEqual(len(self.audit), self.summary['local_count'])
        self.assertEqual(sum(self.summary['status_counts'].values()), len(self.audit))
        self.assertEqual(self.summary['retained_count'] + self.summary['excluded_count'], len(self.audit))

    def test_only_confirmed_or_explicitly_reconciled_codes_are_shipped(self):
        audit_by_code = {row['code']: row for row in self.audit}
        self.assertEqual(len({place['code'] for place in self.catalog}), len(self.catalog))
        for place in self.catalog:
            with self.subTest(code=place['code']):
                self.assertRegex(place['code'], r'^[A-Z]{3}$')
                row = audit_by_code[place['code']]
                self.assertIn(row['status'], ACCEPTED)
                self.assertEqual(row['duffel_id'], place['duffel_id'])
        accepted = {row['code'] for row in self.audit if row['status'] in ACCEPTED}
        self.assertEqual(accepted, {place['code'] for place in self.catalog})

    def test_metadata_exceptions_are_explicit_and_city_conversions_drop_old_geometry(self):
        catalog = {place['code']: place for place in self.catalog}
        for row in self.audit:
            code = row['code']
            if row['status'] == 'confirmed_country_exception':
                self.assertIn('provider_country_differs', catalog[code]['validation_warnings'])
                self.assertEqual(catalog[code]['duffel_country'], row['duffel_country'])
            if row['status'] == 'converted_to_city':
                self.assertEqual(catalog[code]['type'], 'city')
                self.assertNotIn('latitude', catalog[code])
                self.assertNotIn('longitude', catalog[code])
            if row['metadata_warning']:
                self.assertIn('provider_coordinates_differ', catalog[code]['validation_warnings'])
                self.assertEqual(catalog[code]['name'], row['local_name'])


if __name__ == '__main__':
    unittest.main()
