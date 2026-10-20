"""Fixed, bounded five-order protocol; invoked by compare_search_pacing.py --five-order."""
from __future__ import annotations

from collections import deque
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import time

import requests

ORDERS = ('ABCDE', 'BCDEA', 'CDEAB', 'DEABC', 'EABCD')
COUNTERS = ('http_requests', 'flight_get_requests', 'consent_post_requests', 'consent_refetch_requests')
FINISHED = {'ok', 'no_offers', 'error'}
SETTINGS = dict(origin='LIM', destination='MIL', target_date='2026-12-12', date_flex_days=2,
                min_duration=14, max_duration=18, one_way=False, adults=1,
                min_connections_per_slice=0, max_connections_per_slice=2,
                max_results_per_query=20, exclude_self_transfers=True,
                query_encoding='browser', session_mode='shared', request_interval_seconds=1.5)


def utc():
    return datetime.now(timezone.utc).isoformat()


def plan(order):
    rows = []
    for position, group in enumerate(order, 1):
        departure = date(2026, 12, 10 + ord(group) - ord('A'))
        for duration in range(14, 19):
            returning = departure + timedelta(days=duration)
            rows.append(dict(query_id=f'{departure}|{returning}', departure_date=str(departure),
                             return_date=str(returning), group=group, group_position=position,
                             position=len(rows) + 1, status='pending'))
    return rows


def pair(row):
    return {key: row[key] for key in ('query_id', 'departure_date', 'return_date')}


def counts(rows):
    complete = [row for row in rows if row['status'] in FINISHED]
    return dict(attempted=len(complete), priced=sum(r['status'] == 'ok' for r in complete),
                valid_unpriced=sum(r['status'] == 'no_offers' for r in complete),
                errors=sum(r['status'] == 'error' for r in complete),
                interrupted=sum(r['status'] in {'running', 'interrupted'} for r in rows),
                pending=sum(r['status'] == 'pending' for r in rows))


def persist(manifest, output):
    temporary = output / 'comparison.json.tmp'
    temporary.write_text(json.dumps(manifest, indent=2) + '\n')
    temporary.replace(output / 'comparison.json')


class ProtocolError(RuntimeError):
    pass


class PassRecorder:
    def __init__(self, batch, revision, preceding=0):
        self.batch = batch
        self.rows = batch['queries']
        self.revision = revision
        self.preceding = preceding
        self.started = []
        self.finished = []
        self.metadata = None
        self.terminal = False
        self.fatal = False

    def accept(self, event):
        kind = event.get('type')
        if kind == 'start':
            if self.metadata is not None:
                raise ProtocolError('Duplicate start event.')
            for key, expected in dict(region='iad1', revision=self.revision, session_mode='shared',
                                      query_encoding='browser', request_interval_seconds=1.5, total=25).items():
                if event.get(key) != expected:
                    raise ProtocolError(f'Deployment did not confirm {key}.')
            expected_experiment = dict(departure_order=self.batch['departure_order'], continue_generic_errors=True)
            if event.get('experiment') != expected_experiment:
                raise ProtocolError('Deployment did not confirm the bounded experiment settings.')
            if event.get('queries') != [pair(row) for row in self.rows]:
                raise ProtocolError('Advertised query order differs from the experiment plan.')
            if not isinstance(event.get('request_id'), str) or not event['request_id']:
                raise ProtocolError('Missing request ID.')
            self.metadata = {key: event[key] for key in ('request_id', 'region', 'revision', 'session_mode',
                                                         'query_encoding', 'request_interval_seconds')}
            self.batch['metadata'] = self.metadata
        elif kind == 'progress':
            if self.metadata is None or self.terminal:
                raise ProtocolError('Progress outside an active stream.')
            status = event.get('status')
            if status == 'running':
                if self.fatal or len(self.started) != len(self.finished) or len(self.started) >= 25:
                    raise ProtocolError('Overlapping, excessive or post-failure query starts.')
                row = self.rows[len(self.started)]
                if pair(event) != pair(row):
                    raise ProtocolError('Actual query-start order differs from the experiment plan.')
                row.update(status='running', start_event_received_at=utc(),
                           cumulative_started=self.preceding + len(self.started) + 1)
                self.started.append(row['query_id'])
            elif status in FINISHED:
                if len(self.started) != len(self.finished) + 1:
                    raise ProtocolError('Query completion without exactly one running query.')
                row = self.rows[len(self.finished)]
                if pair(event) != pair(row) or event.get('completed') != len(self.finished) + 1 or event.get('total') != 25:
                    raise ProtocolError('Query completion/order/count mismatch.')
                diagnostic = event.get('diagnostics', {})
                if diagnostic.get('session_mode') != 'shared' or diagnostic.get('session_index') != 1:
                    raise ProtocolError('Google session changed within an ordering.')
                if diagnostic.get('query_encoding') != 'browser_v1' or diagnostic.get('request_interval_seconds') != 1.5:
                    raise ProtocolError('Query settings changed within an ordering.')
                for key in ('query_started_at', 'query_finished_at'):
                    stamp = datetime.fromisoformat(diagnostic.get(key, ''))
                    if stamp.tzinfo is None:
                        raise ProtocolError('Query timestamps must include timezone information.')
                if diagnostic['query_finished_at'] < diagnostic['query_started_at']:
                    raise ProtocolError('Query timestamps are reversed.')
                if any(type(diagnostic.get(key)) is not int or diagnostic[key] < 0 for key in COUNTERS):
                    raise ProtocolError('Missing or invalid HTTP request counters.')
                if diagnostic['http_requests'] != diagnostic['flight_get_requests'] + diagnostic['consent_post_requests']:
                    raise ProtocolError('HTTP request counters do not reconcile.')
                row.update(status=status, diagnostics=diagnostic, finished_received_at=utc(),
                           cumulative_completed=self.preceding + len(self.finished) + 1)
                if isinstance(event.get('offer'), dict):
                    row['price'] = event['offer'].get('price')
                self.finished.append(row['query_id'])
                if status == 'error' and not (diagnostic.get('category') == 'upstream_error'
                        and diagnostic.get('http_status') == 200 and diagnostic.get('upstream_error') is True):
                    self.fatal = True
            else:
                raise ProtocolError('Unknown progress outcome.')
        elif kind in {'done', 'error'}:
            if self.metadata is None or self.terminal or len(self.started) != len(self.finished):
                raise ProtocolError('Invalid terminal event or unfinished query.')
            for key in ('region', 'revision', 'request_id', 'session_mode', 'query_encoding', 'request_interval_seconds'):
                if event.get(key) != self.metadata[key]:
                    raise ProtocolError('Terminal deployment metadata changed.')
            expected_counts = {key: sum(r.get('diagnostics', {}).get(key, 0) for r in self.rows) for key in COUNTERS}
            if event.get('request_counts') != expected_counts or event.get('completed') != len(self.finished) or event.get('total') != 25:
                raise ProtocolError('Terminal counts differ from observed queries.')
            for key, status in (('failed_queries', 'error'), ('pending_queries', 'pending')):
                if {q['query_id'] for q in event.get(key, [])} != {r['query_id'] for r in self.rows if r['status'] == status}:
                    raise ProtocolError('Terminal query ledger differs from observations.')
            self.batch.update(terminal=kind, request_counts=expected_counts)
            self.terminal = True
            if kind != 'done' or len(self.finished) != 25:
                self.fatal = True
        elif kind != 'row':
            raise ProtocolError('Unknown event type.')

    def interrupt(self):
        for row in self.rows:
            if row['status'] == 'running':
                row.update(status='interrupted', message='Stream ended before an outcome; availability is unknown.')


def cooldown(manifest, output, seconds):
    interval = dict(started_at=utc(), planned_seconds=seconds, after_ordering=len(manifest['completed_orderings']))
    manifest['cooldowns'].append(interval)
    deadline = time.monotonic() + seconds
    try:
        while True:
            remaining = max(0, deadline - time.monotonic())
            interval['remaining_seconds'] = round(remaining, 1)
            persist(manifest, output)
            print(json.dumps(dict(event='cooldown', remaining_seconds=round(remaining))), flush=True)
            if remaining <= 0:
                break
            time.sleep(min(30, remaining))
    finally:
        interval['finished_at'] = utc()
        persist(manifest, output)


def render_report(manifest, output):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    batches = manifest['orderings']
    title_suffix = '' if manifest['state'] == 'complete' else ' (partial run)'
    rows = [row for batch in batches for row in batch['queries']]
    summary = counts(rows)
    table = ['| Ordering | Planned | Attempted | Priced | Valid unpriced | Errors | Interrupted | Pending | Priced rate |',
             '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for batch in batches:
        c = counts(batch['queries'])
        rate = f"{c['priced']/c['attempted']:.1%}" if c['attempted'] else 'Not observed'
        table.append(f"| {batch['number']}: {batch['order']} | 25 | {c['attempted']} | {c['priced']} | {c['valid_unpriced']} | {c['errors']} | {c['interrupted']} | {c['pending']} | {rate} |")
    by_position = []
    for position in range(1, 6):
        c = counts([r for r in rows if r['group_position'] == position])
        by_position.append(dict(position=position, **c))
    early = {r['query_id']: r for r in rows if r['group_position'] == 1 and r['status'] in FINISHED}
    late = {r['query_id']: r for r in rows if r['group_position'] == 5 and r['status'] in FINISHED}
    matched = sorted(early.keys() & late.keys())
    paired = dict(pairs=len(matched), early_priced=sum(early[k]['status']=='ok' for k in matched),
                  late_priced=sum(late[k]['status']=='ok' for k in matched),
                  lost=sum(early[k]['status']=='ok' and late[k]['status']!='ok' for k in matched),
                  gained=sum(early[k]['status']!='ok' and late[k]['status']=='ok' for k in matched))
    analysis = dict(summary=summary, by_group_position=by_position, paired_early_late=paired)
    (output / 'analysis.json').write_text(json.dumps(analysis, indent=2)+'\n')

    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False})
    figure, axis = plt.subplots(figsize=(10, 4.4), constrained_layout=True)
    window = deque(maxlen=10)
    for batch in batches:
        x, y = [], []
        for row in batch['queries']:
            if row['status'] not in FINISHED:
                continue
            window.append(row['status']=='ok')
            x.append((batch['number']-1)*25 + row['position'])
            y.append(sum(window)/10 if len(window)==10 else float('nan'))
        axis.plot(x, y, color='#15634c', linewidth=2, marker='.', markersize=3)
    for index, pause in enumerate(manifest['cooldowns']):
        at = pause['after_ordering']*25 + .5
        axis.axvline(at, color='#b58322', linestyle='--', alpha=.7,
                     label='Five-minute cooldown' if index==0 else None)
    for batch in batches:
        for row in batch['queries']:
            if row['status'] not in FINISHED:
                at=(batch['number']-1)*25+row['position']
                axis.axvspan(at-.5, at+.5, color='#dddddd', alpha=.45)
    axis.set(xlim=(1,125), ylim=(-.03,1.03), xlabel='Scheduled date-pair check (grey = no completed outcome)',
             ylabel='Priced results / last 10 completed checks', title='Rolling priced-response rate — five rotated orderings' + title_suffix)
    axis.set_yticks([0,.25,.5,.75,1], ['0%','25%','50%','75%','100%'])
    axis.grid(axis='y', alpha=.2)
    if manifest['cooldowns']:
        axis.legend(loc='lower left')
    figure.savefig(output/'rolling-success.svg')
    figure.savefig(output/'rolling-success.png', dpi=160)
    plt.close(figure)

    figure, axis = plt.subplots(figsize=(8,4.4), constrained_layout=True)
    values = [c['priced']/c['attempted'] if c['attempted'] else 0 for c in by_position]
    axis.bar(range(1,6), values, color=['#15634c' if c['attempted'] else '#dddddd' for c in by_position])
    for c, value in zip(by_position, values):
        label = f"{c['priced']}/{c['attempted']}" if c['attempted'] else 'Not observed'
        axis.text(c['position'], value+.025, label, ha='center')
    axis.set(ylim=(0,1.15), xticks=range(1,6), xlabel='Group position within each ordering (five date pairs per group)',
             ylabel='Priced results / attempted pairs', title='Response rate by group position — date groups rotate' + title_suffix)
    axis.set_yticks([0,.25,.5,.75,1], ['0%','25%','50%','75%','100%'])
    axis.grid(axis='y', alpha=.2)
    figure.savefig(output/'group-position-success.svg')
    figure.savefig(output/'group-position-success.png', dpi=160)
    plt.close(figure)
    report = ['# Five-order search experiment', '', f"Run started: {manifest['started_at']}.",
              f"State: **{manifest['state']}**. {summary['attempted']}/125 completed outcomes, "
              f"{summary['priced']} priced, {summary['errors']} errors, {summary['interrupted']} interrupted, {summary['pending']} pending.",
              '', *table, '', '![Rolling success](rolling-success.svg)', '',
              'The rolling window requires ten completed outcomes and carries across cooldowns. Lines break at ordering boundaries. Grey areas have no completed outcome.',
              '', '![Group position success](group-position-success.svg)', '',
              f"Matched early/late comparison: {paired['pairs']} identical date pairs; "
              f"{paired['early_priced']} priced early versus {paired['late_priced']} late; "
              f"{paired['lost']} lost prices, {paired['gained']} gained prices.", '',
              'Pending/interrupted outcomes are excluded from denominators. Valid unpriced responses are reported separately and do not establish absent flights.', '',
              'This is descriptive evidence, not proof of throttling. Cache warming, changing Google responses and unrelated production traffic remain possible explanations. '
              'Session renewal and cooldown occur together, so their individual effects cannot be separated. Five orderings are not 125 independent experimental replications.', '',
              f"Stop reason: {manifest.get('stop_reason') or 'None'}.", '',
              'Reproducible settings and safe observations: comparison.json; analysis: analysis.json; raw API events: ordering-*.ndjson.',
              'HTTP counters measure explicit client calls, including consent POSTs/refetches, not redirects handled internally by the HTTP library.']
    (output/'report.md').write_text('\n'.join(report)+'\n')


def run_five_order(args, password):
    # Plotting is local tooling only, never bundled into the production requirements.
    import matplotlib  # noqa: F401
    if not args.expected_revision or len(args.expected_revision) != 40:
        raise ValueError('--five-order requires the exact 40-character deployed --expected-revision.')
    if args.expected_region not in {None, 'iad1'}:
        raise ValueError('The five-order protocol fixes the region to iad1.')
    if args.cooldown_seconds not in {None, 300}:
        raise ValueError('The five-order protocol requires a 300-second cooldown.')
    if args.encodings != 'browser' or args.session_modes != 'shared' or args.intervals != '4,1.5,8,8,1.5,4':
        raise ValueError('The five-order protocol fixes pacing, encoding and session mode; omit their override flags.')
    output = Path(args.output_dir or 'work/diagnostics/five-order-comparison')
    output.mkdir(parents=True, exist_ok=True)
    if (output/'comparison.json').exists():
        raise ValueError('Output already contains a run; choose a new directory to preserve prior evidence.')
    batches=[]
    for number, order in enumerate(ORDERS, 1):
        rows=plan(order)
        batches.append(dict(number=number, order=order, queries=rows,
                            departure_order=[rows[index]['departure_date'] for index in range(0,25,5)],
                            state='pending'))
    manifest=dict(protocol='five_order_v1', started_at=utc(), state='running', settings=SETTINGS,
                  base_url=args.base_url, expected_region='iad1', expected_revision=args.expected_revision,
                  orderings=batches, cooldowns=[], completed_orderings=[])
    persist(manifest,output)
    active=None
    old_handlers={}
    def cancel(signum, frame):
        raise KeyboardInterrupt
    shutdown_signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, 'SIGHUP'):
        shutdown_signals.append(signal.SIGHUP)
    for sig in shutdown_signals:
        old_handlers[sig]=signal.signal(sig,cancel)
    try:
        for batch in batches:
            if batch['number']>1:
                cooldown(manifest,output,300)
            active=PassRecorder(batch,args.expected_revision,
                                preceding=sum(counts(b['queries'])['attempted'] for b in batches))
            batch.update(state='running', started_at=utc())
            experiment=dict(departure_order=batch['departure_order'], continue_generic_errors=True)
            payload=dict(SETTINGS,password=password,experiment=experiment)
            print(json.dumps(dict(event='ordering_start', number=batch['number'], order=batch['order'])),flush=True)
            with (output/f"ordering-{batch['number']}.ndjson").open('w') as log:
                with requests.post(args.base_url.rstrip('/')+'/api/search',json=payload,stream=True,timeout=(15,60)) as response:
                    response.raise_for_status()
                    for line in response.iter_lines(chunk_size=1):
                        if not line:
                            continue
                        event=json.loads(line)
                        log.write(json.dumps(event)+'\n');log.flush()
                        active.accept(event)
                        persist(manifest,output)
                        if event.get('type')=='progress' and event.get('status') in FINISHED:
                            print(json.dumps(dict(event='query_finished', ordering=batch['number'],
                                                  **counts(batch['queries']))),flush=True)
                        if active.terminal:
                            break
            if not active.terminal:
                raise ProtocolError('Stream ended without a terminal event.')
            if active.fatal:
                raise ProtocolError('Ordering stopped on a fatal response; remaining orderings were not attempted.')
            batch.update(state='complete',finished_at=utc())
            manifest['completed_orderings'].append(batch['number'])
            persist(manifest,output)
            render_report(manifest,output)
            print(json.dumps(dict(event='ordering_complete', number=batch['number'], **counts(batch['queries']))),flush=True)
        manifest['state']='complete'
    except KeyboardInterrupt:
        manifest.update(state='cancelled',stop_reason='Cancelled by user/process signal; no automatic resume.')
    except Exception as exc:
        manifest.update(state='stopped',stop_reason=f'{type(exc).__name__}: {exc}')
    finally:
        if active and active.batch['state']=='running':
            active.interrupt()
            active.batch.update(state='stopped',finished_at=utc())
        manifest['finished_at']=utc()
        persist(manifest,output)
        render_report(manifest,output)
        for sig, previous in old_handlers.items():
            signal.signal(sig,previous)
    print(json.dumps(dict(event='experiment_finished',state=manifest['state'],
                          stop_reason=manifest.get('stop_reason'), **counts([r for b in batches for r in b['queries']]))),flush=True)
    return 0 if manifest['state']=='complete' else 2
