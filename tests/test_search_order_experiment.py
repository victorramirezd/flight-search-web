import copy
from datetime import date, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from api.search import experiment_plan
from fast_flights.exceptions import FlightsNotFound
from flight_search import ApiError, ProviderUnavailableError, build_search_config, search_queries
from google_flights import GoogleFlightsClient
from scripts.search_order_experiment import (COUNTERS, ORDERS, PassRecorder, ProtocolError,
                                            counts, cooldown, pair, plan, render_report)
from test_search_queries import endpoint, post

REVISION = 'a' * 40
STAMP = '2026-10-20T10:00:00+00:00'
GENERIC = '<script class="ds:1">data:errorHasStatus: true,</script>'


def batch(number=1):
    order = ORDERS[number-1]
    rows = plan(order)
    return dict(number=number,order=order,queries=rows,
                departure_order=[rows[i]['departure_date'] for i in range(0,25,5)],state='running')


def start(b):
    return dict(type='start', queries=[pair(r) for r in b['queries']], total=25,
                region='iad1', revision=REVISION, session_mode='shared', query_encoding='browser',
                request_interval_seconds=1.5,request_id='request-id',
                experiment=dict(departure_order=b['departure_order'],continue_generic_errors=True))


def finish(row, index, status='ok'):
    return dict(type='progress', **pair(row),status=status,completed=index,total=25,
                diagnostics=dict(session_mode='shared',session_index=1,query_encoding='browser_v1',
                                 request_interval_seconds=1.5,query_started_at=STAMP,query_finished_at=STAMP,
                                 http_requests=1,flight_get_requests=1,consent_post_requests=0,
                                 consent_refetch_requests=0,category='upstream_error' if status=='error' else 'success',
                                 http_status=200,upstream_error=status=='error'))


def complete(recorder, row, index, status='ok'):
    recorder.accept(dict(type='progress',**pair(row),status='running'))
    recorder.accept(finish(row,index,status))


class OrderExperimentTest(unittest.TestCase):
    def test_balanced_rotations_each_pair_once_at_each_group_position(self):
        seen={}
        for order in ORDERS:
            rows=plan(order)
            self.assertEqual(len(rows),25)
            self.assertEqual(len({r['query_id'] for r in rows}),25)
            for row in rows:
                seen.setdefault(row['query_id'],[]).append(row['group_position'])
        self.assertEqual(len(seen),25)
        self.assertTrue(all(sorted(positions)==[1,2,3,4,5] for positions in seen.values()))

    def test_experiment_validation_and_chronological_returns(self):
        cfg=build_search_config(origin='LIM',destination='MIL',target_date='2026-12-12',
                                date_flex_days=2,min_duration=14,max_duration=18)
        queries=search_queries(cfg)
        original=list(queries)
        order=batch(3)['departure_order']
        experiment=dict(departure_order=order,continue_generic_errors=True)
        reordered=experiment_plan(experiment,queries)
        self.assertEqual([q.payload() for q in reordered],[pair(r) for r in plan(ORDERS[2])])
        self.assertEqual(queries,original)
        for invalid in (None,{},[],{**experiment,'extra':True},
                        dict(experiment,continue_generic_errors='true'),
                        dict(experiment,departure_order=order[:-1]),
                        dict(experiment,departure_order=order[:-1]+[order[0]]),
                        dict(experiment,departure_order=order[:-1]+['2026-12-30']),
                        dict(experiment,departure_order=[[],*order[1:]])):
            with self.subTest(value=invalid),self.assertRaises(ValueError):
                experiment_plan(invalid,queries)
        with self.assertRaisesRegex(ValueError,'25'):
            experiment_plan(experiment,queries+queries)

    def test_backend_generic_continuation_is_bounded_and_recognized_only(self):
        def fetch(query,*,integration):
            integration.fetch_html(query)
            raise FlightsNotFound('error')
        network=Mock()
        network.get.return_value=SimpleNamespace(status_code=200,text=GENERIC)
        with patch('google_flights.Client',return_value=network),patch('google_flights.get_flights',side_effect=fetch),patch('google_flights.time.sleep'):
            client=GoogleFlightsClient(continue_generic_errors=True)
            for index in range(25):
                with self.assertRaises(ApiError) as error:
                    client.search_offers(origin='LIM',destination='MIL',departure_date=date(2026,12,10),
                                         return_date=date(2026,12,24),adults=1,max_results=20)
                self.assertNotIsInstance(error.exception,ProviderUnavailableError)
                self.assertIn('query_started_at',error.exception.diagnostics)
            self.assertEqual(network.get.call_count,25)
            with self.assertRaisesRegex(ProviderUnavailableError,'25-query'):
                client.search_offers(origin='LIM',destination='MIL',departure_date=date(2026,12,10),
                                     return_date=None,adults=1,max_results=20)
            self.assertEqual(network.get.call_count,25)
            network.get.return_value=SimpleNamespace(status_code=200,text='<script class="ds:1">data:[],</script>')
            client=GoogleFlightsClient(continue_generic_errors=True)
            with self.assertRaisesRegex(ProviderUnavailableError,'unrecognized'):
                client.search_offers(origin='LIM',destination='MIL',departure_date=date(2026,12,10),
                                     return_date=None,adults=1,max_results=20)

    def test_endpoint_actual_order_and_validation_before_streaming(self):
        b=batch(2)
        payload=dict(password='victor',origin='LIM',destination='MIL',target_date='2026-12-12',
                     date_flex_days=2,min_duration=14,max_duration=18,
                     experiment=dict(departure_order=b['departure_order'],continue_generic_errors=True))
        with endpoint() as address,patch('api.search.GoogleFlightsClient') as Client,patch('api.search.sys.stderr'):
            Client.return_value.search_offers.return_value=[]
            status,body=post(address,payload)
            self.assertEqual(status,200)
            events=[json.loads(line) for line in body.splitlines()]
            expected=[pair(r) for r in b['queries']]
            self.assertEqual(events[0]['queries'],expected)
            self.assertEqual([pair(e) for e in events if e['type']=='progress' and e['status']=='running'],expected)
            self.assertEqual([c.kwargs['departure_date'].isoformat()+'|'+c.kwargs['return_date'].isoformat()
                              for c in Client.return_value.search_offers.call_args_list],[r['query_id'] for r in expected])
            self.assertIs(Client.call_args.kwargs['continue_generic_errors'],True)
            Client.reset_mock()
            status,_=post(address,dict(payload,experiment=dict(payload['experiment'],departure_order=[])))
            self.assertEqual(status,400)
            Client.assert_not_called()
            normal={k:v for k,v in payload.items() if k!='experiment'}
            status,body=post(address,normal)
            self.assertEqual(status,200)
            events=[json.loads(line) for line in body.splitlines()]
            self.assertIs(Client.call_args.kwargs['continue_generic_errors'],False)
            self.assertEqual(events[0]['queries'],[pair(r) for r in plan('ABCDE')])
            self.assertNotIn('experiment',events[0])

    def test_recorder_checks_metadata_plan_actual_order_and_counts(self):
        for key,value in [('region','fra1'),('revision','b'*40),('session_mode','fresh'),('experiment',{})]:
            b=batch();rec=PassRecorder(b,REVISION)
            with self.subTest(key=key),self.assertRaises(ProtocolError):
                rec.accept(dict(start(b),**{key:value}))
        b=batch();rec=PassRecorder(b,REVISION);rec.accept(start(b))
        with self.assertRaisesRegex(ProtocolError,'Actual'):
            rec.accept(dict(type='progress',**pair(b['queries'][1]),status='running'))
        complete(rec,b['queries'][0],1)
        self.assertEqual(counts(b['queries'])['attempted'],1)
        with self.assertRaises(ProtocolError):
            rec.accept(finish(b['queries'][1],2))

    def test_terminal_reconciliation_fatal_stops_and_interrupted_denominators(self):
        b=batch();rec=PassRecorder(b,REVISION);metadata=start(b);rec.accept(metadata)
        for index,row in enumerate(b['queries'],1):
            complete(rec,row,index,'error' if index==2 else 'ok')
        totals={key:sum(r['diagnostics'][key] for r in b['queries']) for key in COUNTERS}
        terminal=dict(metadata,type='done',completed=25,request_counts=totals,
                      failed_queries=[pair(b['queries'][1])],pending_queries=[])
        with self.assertRaises(ProtocolError):
            rec.accept(dict(terminal,request_counts={}))
        rec.accept(terminal)
        self.assertTrue(rec.terminal);self.assertFalse(rec.fatal)
        b=batch();rec=PassRecorder(b,REVISION);rec.accept(start(b))
        rec.accept(dict(type='progress',**pair(b['queries'][0]),status='running'))
        bad=finish(b['queries'][0],1,'error');bad['diagnostics'].update(category='http_rate_limited',http_status=429)
        rec.accept(bad)
        self.assertTrue(rec.fatal)
        with self.assertRaises(ProtocolError):
            rec.accept(dict(type='progress',**pair(b['queries'][1]),status='running'))
        b=batch();rec=PassRecorder(b,REVISION);rec.accept(start(b))
        rec.accept(dict(type='progress',**pair(b['queries'][0]),status='running'))
        rec.interrupt();c=counts(b['queries'])
        self.assertEqual((c['attempted'],c['interrupted'],c['pending']),(0,1,24))

    def test_partial_report_charts_and_paired_denominators(self):
        batches=[batch(i) for i in range(1,6)]
        rows=batches[0]['queries']
        for row in rows[:10]:row.update(status='ok')
        rows[10]['status']='interrupted'
        manifest=dict(started_at=STAMP,state='cancelled',orderings=batches,cooldowns=[],stop_reason='cancelled')
        with TemporaryDirectory() as directory:
            output=Path(directory)
            render_report(manifest,output)
            analysis=json.loads((output/'analysis.json').read_text())
            self.assertEqual(analysis['summary']['attempted'],10)
            self.assertEqual(analysis['summary']['pending'],114)
            self.assertEqual(analysis['paired_early_late']['pairs'],0)
            self.assertEqual(analysis['by_group_position'][2]['attempted'],0)
            self.assertTrue((output/'rolling-success.svg').exists())
            self.assertTrue((output/'group-position-success.png').exists())
            self.assertIn('Not observed',(output/'report.md').read_text())

    def test_cooldown_is_interruptible_and_preserves_partial_timing(self):
        manifest=dict(cooldowns=[],completed_orderings=[1])
        with TemporaryDirectory() as directory,patch('scripts.search_order_experiment.time.monotonic',side_effect=[0,0]),patch('scripts.search_order_experiment.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                cooldown(manifest,Path(directory),300)
            self.assertIn('finished_at',manifest['cooldowns'][0])
            self.assertEqual(manifest['cooldowns'][0]['remaining_seconds'],300)
