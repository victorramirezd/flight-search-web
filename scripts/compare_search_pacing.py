#!/usr/bin/env python3
"""Compare bounded, sequential production pacing/encoding passes without automatic retries."""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='https://www.travelling-alpaca.com')
    parser.add_argument('--intervals', default='4,1.5,8,8,1.5,4')
    parser.add_argument('--encodings', default='browser', help='One mode for all passes or comma-separated browser/legacy modes matching intervals.')
    parser.add_argument('--cooldown-seconds', type=float, default=30)
    parser.add_argument('--output-dir', default='work/diagnostics/pacing-comparison')
    args = parser.parse_args()
    password = os.environ.get('FLIGHT_SEARCH_PASSWORD')
    if not password:
        parser.error('Set FLIGHT_SEARCH_PASSWORD in the environment.')
    intervals = [float(value) for value in args.intervals.split(',')]
    if not intervals or any(not 1.5 <= value <= 15 for value in intervals):
        parser.error('Intervals must be between 1.5 and 15 seconds.')
    encodings = args.encodings.split(',')
    if len(encodings) == 1:
        encodings *= len(intervals)
    if len(encodings) != len(intervals) or any(mode not in {'browser', 'legacy'} for mode in encodings):
        parser.error('Encodings must be browser/legacy and match the number of intervals.')
    if not 0 <= args.cooldown_seconds <= 120:
        parser.error('Cooldown must be between 0 and 120 seconds.')
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    queries = [dict(departure_date=date(2026, 12, day).isoformat(),
                    return_date=(date(2026, 12, day) + timedelta(days=duration)).isoformat())
               for day in (10, 11, 12) for duration in (14, 15, 16)]
    public_settings = dict(origin='LIM', destination='MIL', target_date='2026-12-16',
                           date_flex_days=6, one_way=False, min_duration=14, max_duration=16,
                           adults=1, min_connections_per_slice=0, max_connections_per_slice=2,
                           max_results_per_query=20, exclude_self_transfers=True,
                           retry_queries=queries)
    manifest = dict(started_at=datetime.now(timezone.utc).isoformat(),
                    base_url=args.base_url, intervals=intervals, encodings=encodings, cooldown_seconds=args.cooldown_seconds,
                    settings=public_settings, passes=[])
    for index, (interval, encoding) in enumerate(zip(intervals, encodings), 1):
        if index > 1:
            print(json.dumps(dict(event='cooldown', seconds=args.cooldown_seconds)), flush=True)
            time.sleep(args.cooldown_seconds)
        print(json.dumps(dict(event='pass_start', pass_number=index, interval_seconds=interval, query_encoding=encoding)), flush=True)
        payload = dict(public_settings, password=password, request_interval_seconds=interval, query_encoding=encoding)
        started = time.monotonic()
        events = []
        stop = False
        with (output / f'pass-{index}-{encoding}-{interval:g}s.ndjson').open('w') as log:
            with requests.post(args.base_url.rstrip('/') + '/api/search', json=payload,
                               stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                for line in response.iter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    events.append(event)
                    log.write(json.dumps(event) + '\n')
                    log.flush()
                    if event['type'] == 'start':
                        if event.get('query_encoding') != encoding:
                            raise RuntimeError('Production did not confirm the requested encoding; comparison aborted.')
                        if event.get('request_interval_seconds') != interval:
                            raise RuntimeError('Production did not confirm the requested interval; comparison aborted.')
                    if event['type'] in {'done', 'error'}:
                        stop = (event.get('diagnostics') or {}).get('category') in {
                            'http_rate_limited', 'http_error', 'missing_results_script', 'consent_error'}
                        break
        finished = [e for e in events if e['type'] == 'progress' and e['status'] != 'running']
        terminal = next((e for e in reversed(events) if e['type'] in {'done', 'error'}), {})
        metadata = next((e for e in events if e['type'] == 'start'), {})
        summary = dict(pass_number=index, interval_seconds=interval, query_encoding=encoding,
                       duration_seconds=round(time.monotonic() - started, 2),
                       attempted=len(finished), planned=len(queries),
                       priced=sum(e['status'] == 'ok' for e in finished),
                       empty=sum(e['status'] == 'no_offers' for e in finished),
                       errors=sum(e['status'] == 'error' for e in finished),
                       unattempted=len(queries) - len(finished),
                       terminal=terminal.get('type', 'interrupted'),
                       request_id=metadata.get('request_id'), region=metadata.get('region'),
                       revision=metadata.get('revision'),
                       outcomes=[dict(departure=e['departure_date'], returning=e['return_date'],
                                      status=e['status'], diagnostics=e.get('diagnostics', {})) for e in finished])
        manifest['passes'].append(summary)
        (output / 'comparison.json').write_text(json.dumps(manifest, indent=2) + '\n')
        print(json.dumps({k: v for k, v in summary.items() if k != 'outcomes'}), flush=True)
        if stop or not terminal:
            print('Comparison stopped after an HTTP/blocking response or interrupted stream.', flush=True)
            break
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
