import os
import unittest
from unittest.mock import patch

from app import server_options
from api.search import deployment_metadata


class RenderHostingTest(unittest.TestCase):
    def test_local_binding_stays_private_until_explicitly_overridden(self):
        self.assertEqual(server_options([]).host, '127.0.0.1')
        options = server_options(['--host', '0.0.0.0', '--port', '10000'])
        self.assertEqual((options.host, options.port), ('0.0.0.0', 10000))

    def test_only_safe_deployment_identifiers_are_exposed(self):
        with patch.dict(os.environ, {'FLIGHT_SEARCH_REGION':'render-virginia',
                                     'RENDER_GIT_COMMIT':'a'*40}, clear=True):
            self.assertEqual(deployment_metadata(), {'region':'render-virginia', 'revision':'a'*40})
            os.environ['VERCEL_REGION']='iad1'
            os.environ['VERCEL_GIT_COMMIT_SHA']='b'*40
            self.assertEqual(deployment_metadata(), {'region':'iad1', 'revision':'b'*40})
        with patch.dict(os.environ, {'FLIGHT_SEARCH_REGION':'secret<private>',
                                     'RENDER_GIT_COMMIT':'secret?private'}, clear=True):
            self.assertEqual(deployment_metadata(), {'region':'local', 'revision':None})
