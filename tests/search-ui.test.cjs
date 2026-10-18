const assert = require('node:assert/strict');
const { addDays, windows, timelineScale, calendarData, queryPlan, SearchState, tableClipboard, writeClipboard } = require('../search-ui.js');
const config = { origin: 'MIL', destination: 'LON', target_date: '2027-02-04', date_flex_days: 4,
  min_duration: 12, max_duration: 17, one_way: false, adults: 1 };
const range = windows(config);
assert.deepEqual(range, { start: '2027-01-31', end: '2027-02-08', returnStart: '2027-02-12', returnEnd: '2027-02-25' });
assert.equal(queryPlan(config).length, 54);
assert.equal(addDays('2028-02-28', 1), '2028-02-29');
assert.equal(addDays('2027-12-31', 1), '2028-01-01');
assert.equal(addDays('2027-02-30', 1), null);
assert.equal(windows({ ...config, one_way: true }).returnStart, null);
assert.equal(queryPlan({ ...config, one_way: true }).length, 9);
assert.equal(queryPlan({ ...config, target_date: '' }).length, 0);

const scale = timelineScale(config);
assert.equal(scale.start, '2027-01-31');
assert.equal(scale.end, '2027-02-25');
assert.equal(scale.departure.left, 0);
assert.equal(scale.departure.width, 9 / 26 * 100);
assert.equal(scale.returning.left, 12 / 26 * 100);
assert.equal(scale.returning.width, 14 / 26 * 100);
assert.equal(scale.overlap, null);
const overlapping = timelineScale({ ...config, min_duration: 2, max_duration: 5 });
assert.equal(overlapping.overlap.start, '2027-02-02');
assert.equal(overlapping.overlap.end, '2027-02-08');
assert.equal(overlapping.overlap.left, 2 / 14 * 100);
assert.equal(overlapping.overlap.width, 7 / 14 * 100);
const oneDayScale = timelineScale({ ...config, one_way: true, date_flex_days: 0 });
assert.deepEqual(oneDayScale.departure, { left: 0, width: 100 });
assert.equal(oneDayScale.returning, null);
assert.equal(oneDayScale.overlap, null);
assert.equal(timelineScale({ ...config, target_date: '' }), null);

const cfg = { ...config, date_flex_days: 0, max_duration: 13 };
const state = new SearchState(cfg);
const [first, second] = queryPlan(cfg);
state.begin([first, second]);
state.progress({ ...first, status: 'running' });
assert.equal(state.counts().completed, 0);
state.progress({ ...first, status: 'ok' });
state.mergeRow({ departure_date: first.departure_date, best_return_date: first.return_date,
  best_price: '688.00', currency: 'EUR', airline_url: 'https://www.google.com/travel/flights?example=1', searched_at: 'original' });
assert.equal(state.displayRows()[0].search_status, 'partial');
state.progress({ ...second, status: 'error', message: 'Query unavailable' });
state.finish();
assert.equal(state.displayRows()[0].search_status, 'partial');
assert.equal(state.failedQueries().length, 1);
assert.equal(state.counts().completed, 2);
cfg.adults = 2;
assert.equal(state.config.adults, 1, 'retry settings must be the submitted snapshot');
state.begin(state.failedQueries(), true);
assert.equal(state.counts().passCompleted, 0);
assert.equal(state.counts().completed, 2, 'original progress must not go backwards during retry');
assert.equal(state.displayRows()[0].search_status, 'partial', 'failure remains unresolved while retry is pending');
state.progress({ ...second, status: 'no_offers' });
state.mergeRow({ departure_date: second.departure_date, best_price: 'N/A' });
state.finish();
assert.equal(state.displayRows()[0].best_price, '688.00', 'empty retry must retain earlier fare');
assert.equal(state.displayRows()[0].search_status, 'ok');
assert.equal(state.failedQueries().length, 0);
assert.throws(() => state.begin([second], true), /Only failed date pairs/);
state.mergeRow({ departure_date: first.departure_date, best_price: '700.00' });
assert.equal(state.displayRows()[0].best_price, '688.00');
state.mergeRow({ departure_date: first.departure_date, best_price: '600.00', searched_at: 'new' });
assert.equal(state.displayRows()[0].best_price, '600.00');
assert.equal(state.displayRows()[0].searched_at, 'new');

const stopped = new SearchState(config);
stopped.begin(queryPlan(config));
stopped.progress({ ...queryPlan(config)[0], status: 'running' });
stopped.finish(true);
assert.equal(stopped.counts().completed, 1);
assert.equal(stopped.counts().failed, 1);
assert.equal([...stopped.ledger.values()].filter(q => q.status === 'pending').length, 53);
assert.equal(stopped.active, false);

// Repeated manual retries remain available while errors remain, with one active pass.
const repeated = new SearchState({ ...config, one_way: true, date_flex_days: 0 });
const only = queryPlan(repeated.config)[0];
repeated.begin([only]);
repeated.progress({ ...only, status: 'error', message: 'Initial failure' });
repeated.finish();
for (let attempt = 1; attempt <= 3; attempt++) {
  repeated.begin(repeated.failedQueries(), true);
  assert.equal(repeated.retryCount, attempt);
  assert.throws(() => repeated.begin([only], true), /already running/);
  repeated.progress({ ...only, status: 'running' });
  repeated.progress({ ...only, status: attempt < 3 ? 'error' : 'ok',
    message: attempt < 3 ? 'Still unavailable' : '',
    offer: attempt < 3 ? null : { price: 700, currency: 'EUR', searched_at: 'latest' } });
  repeated.finish();
  assert.equal(repeated.counts().completed, 1);
  assert.equal(repeated.failedQueries().length, attempt < 3 ? 1 : 0);
}
assert.ok(repeated.lastFinishedAt);
assert.equal(calendarData(repeated).priced, 1);
assert.deepEqual(calendarData(repeated).returns, [null]);

// Every valid date pair has its own price, not only the departure's cheapest summary.
const matrix = new SearchState(config);
const plan = queryPlan(config);
matrix.begin(plan);
const quote = price => ({ price, currency: 'EUR', airlines: 'Example', searched_at: 'now' });
matrix.progress({ ...plan[0], status: 'ok', offer: quote(500) });
matrix.progress({ ...plan[1], status: 'ok', offer: quote(700) });
matrix.progress({ ...plan[2], status: 'ok', offer: quote(500) });
matrix.progress({ ...plan[3], status: 'error', message: 'Unknown availability' });
matrix.progress({ ...plan[4], status: 'no_offers', offer: null });
matrix.progress({ ...plan[5], status: 'running' });
const calendar = calendarData(matrix);
assert.equal(calendar.rows.length, 9);
assert.equal(calendar.returns.length, 14);
assert.equal(calendar.rows.flatMap(row => row.cells).filter(cell => cell.planned).length, 54);
assert.equal(calendar.rows[0].cells[0].price, 500);
assert.equal(calendar.rows[0].cells[1].price, 700);
assert.equal(calendar.rows[0].cells[0].best, true);
assert.equal(calendar.rows[0].cells[2].best, true, 'all equal lowest fares are highlighted');
assert.notEqual(calendar.rows[0].cells[0].color, calendar.rows[0].cells[1].color);
assert.equal(calendar.rows[0].cells[3].status, 'error');
assert.equal(calendar.rows[0].cells[3].price, null);
assert.equal(calendar.rows[0].cells[4].status, 'no_offers');
assert.equal(calendar.rows[0].cells[5].status, 'running');
assert.equal(calendar.rows[0].cells[6].planned, false);
assert.equal(calendar.min, 500);
assert.equal(calendar.max, 700);
assert.equal(calendar.priced, 3);
matrix.finish(true);
matrix.begin(matrix.failedQueries(), true);
matrix.progress({ ...plan[3], status: 'ok', offer: quote(400) });
matrix.finish(true);
assert.equal(calendarData(matrix).min, 400);
assert.equal(calendarData(matrix).rows[0].cells[0].best, false);
assert.equal(calendarData(matrix).rows[0].cells[3].best, true);
assert.equal(calendarData(matrix).rows[0].cells[0].price, 500, 'retry retains earlier successful prices');
// Duffel account currency is carried into the heatmap without conversion.
const sterling = new SearchState({ ...config, one_way: true, date_flex_days: 0 });
sterling.begin([only]);
sterling.progress({ ...only, status: 'ok', offer: { ...quote(120), currency: 'GBP' } });
sterling.mergeRow({ departure_date: only.departure_date, best_price: '120.00', currency: 'GBP' });
assert.equal(calendarData(sterling).currency, 'GBP');
assert.equal(calendarData(sterling).rows[0].cells[0].price, 120);
sterling.progress({ ...only, status: 'ok', offer: { ...quote(1), currency: 'USD' } });
assert.equal(calendarData(sterling).rows[0].cells[0].price, 120, 'unlike currencies are not compared');

const emptyMatrix = calendarData(new SearchState(config));
assert.equal(emptyMatrix.min, null);
assert.equal(emptyMatrix.max, null);
assert.equal(emptyMatrix.priced, 0);
const zeroFare = new SearchState({ ...config, one_way: true, date_flex_days: 0 });
zeroFare.begin([only]);
zeroFare.progress({ ...only, status: 'ok', offer: quote(0) });
assert.equal(calendarData(zeroFare).min, 0);
assert.equal(calendarData(zeroFare).rows[0].cells[0].best, true);

const columns = [['departure_date', 'Departure'], ['best_price', 'Price'], ['currency', 'Currency'],
  ['search_message', 'Search status'], ['airline_url', 'View flights']];
const row = { departure_date: '2027-02-04', best_price: '688.00', currency: 'EUR',
  search_message: 'Partial\tresults\nremain', airline_url: 'https://www.google.com/travel/flights?a=1&b=2' };
const payload = tableClipboard([row], columns);
assert.equal(payload.text.split('\n').length, 2);
assert.equal(payload.text.split('\n')[1].split('\t').length, 5);
assert.ok(payload.text.includes('688.00\tEUR\tPartial results remain'));
assert.ok(payload.text.includes(row.airline_url));
assert.ok(payload.html.includes('x:num="688"'));
assert.ok(payload.html.includes('a=1&amp;b=2'));
assert.ok(!payload.html.includes('<button'));
assert.ok(tableClipboard([{ ...row, search_message: '=unsafe()' }], columns).text.includes("'=unsafe()"));

(async () => {
  const writes = [];
  class Item { constructor(data) { this.data = data; } }
  await writeClipboard(payload, { async write(items) { writes.push(items); } }, Item, Blob);
  assert.equal(await writes[0][0].data['text/html'].text(), payload.html);
  assert.equal(await writes[0][0].data['text/plain'].text(), payload.text);
  let text;
  await writeClipboard(payload, { async write() { throw Error('denied rich content'); },
    async writeText(value) { text = value; } }, Item, Blob);
  assert.equal(text, payload.text);
  await assert.rejects(writeClipboard(payload, { async writeText() { throw Error('denied'); } }, null), /denied/);
  console.log('Date windows, query state, targeted retry, fare merging and Excel clipboard checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
