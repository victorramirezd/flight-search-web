/* Shared search state, date arithmetic, streaming, and Excel clipboard formats. */
(function (root) {
  const finished = new Set(['ok', 'no_offers', 'error']);
  const effectiveStatus = q => finished.has(q.status) ? q.status : q.previous_status || q.status;
  const diagnosticKeys = ['category', 'http_status', 'response_kind', 'consent_handled', 'elapsed_ms',
    'query_encoding', 'request_interval_seconds', 'start_gap_ms', 'exception_type', 'upstream_error', 'consecutive_errors', 'retry_after_seconds', 'result_count', 'google_error_payload'];
  const safeDiagnostics = value => Object.fromEntries(diagnosticKeys
    .filter(key => value && ['string', 'number', 'boolean'].includes(typeof value[key]))
    .map(key => [key, value[key]]));
  const queryId = q => `${q.departure_date}|${q.return_date || 'one-way'}`;
  function addDays(iso, days) {
    if (!/^\d{4}-\d{2}-\d{2}$/.test(iso || '') || !Number.isInteger(days)) return null;
    const value = new Date(`${iso}T00:00:00Z`);
    if (!Number.isFinite(value.getTime()) || value.toISOString().slice(0, 10) !== iso) return null;
    value.setUTCDate(value.getUTCDate() + days);
    return value.toISOString().slice(0, 10);
  }
  function windows(config) {
    const start = addDays(config.target_date, -config.date_flex_days);
    const end = addDays(config.target_date, config.date_flex_days);
    if (!start || !end || config.date_flex_days < 0) return null;
    return { start, end, returnStart: config.one_way ? null : addDays(start, config.min_duration),
      returnEnd: config.one_way ? null : addDays(end, config.max_duration) };
  }
  function queryPlan(config) {
    const dates = windows(config);
    if (!dates) return [];
    const queries = [];
    for (let departure = dates.start; departure <= dates.end; departure = addDays(departure, 1)) {
      const durations = config.one_way ? [0] : Array.from(
        { length: Math.max(0, config.max_duration - config.min_duration + 1) }, (_, i) => config.min_duration + i);
      for (const duration of durations) {
        const query = { departure_date: departure, return_date: config.one_way ? null : addDays(departure, duration) };
        queries.push({ ...query, query_id: queryId(query) });
      }
    }
    return queries;
  }
  const daysBetween = (start, end) => Math.round((Date.parse(`${end}T00:00:00Z`) - Date.parse(`${start}T00:00:00Z`)) / 86400000);
  function timelineScale(config) {
    const dates = windows(config);
    if (!dates) return null;
    const start = dates.returnStart && dates.returnStart < dates.start ? dates.returnStart : dates.start;
    const end = dates.returnEnd && dates.returnEnd > dates.end ? dates.returnEnd : dates.end;
    const days = daysBetween(start, end) + 1;
    function band(first, last) {
      return first && last && first <= last ? { left: daysBetween(start, first) / days * 100,
        width: (daysBetween(first, last) + 1) / days * 100 } : null;
    }
    const overlapStart = dates.returnStart && dates.returnStart > dates.start ? dates.returnStart : dates.start;
    const overlapEnd = dates.returnEnd && dates.returnEnd < dates.end ? dates.returnEnd : dates.end;
    const overlap = !config.one_way && dates.returnStart && overlapStart <= overlapEnd
      ? { start: overlapStart, end: overlapEnd, ...band(overlapStart, overlapEnd) } : null;
    return { start, end, midpoint: addDays(start, Math.floor((days - 1) / 2)),
      departure: band(dates.start, dates.end), returning: band(dates.returnStart, dates.returnEnd), overlap,
      target: (daysBetween(start, config.target_date) + 0.5) / days * 100 };
  }
  const hasPrice = row => row && row.best_price !== '' && row.best_price !== null
    && Number.isFinite(Number(row.best_price)) && Number(row.best_price) >= 0;

  class SearchState {
    constructor(config) {
      this.config = Object.freeze({ ...config });
      this.ledger = new Map(queryPlan(config).map(q => [q.query_id, { ...q, status: 'pending', message: '' }]));
      this.rows = new Map();
      this.active = false;
      this.retryCount = 0;
      this.lastFinishedAt = null;
      this.retrying = false;
      this.resuming = false;
      this.resumeCount = 0;
      this.requests = [];
      this.currentRequest = null;
      this.passIds = new Set();
    }
    begin(queries, retry = false) {
      if (this.active) throw new Error('A search is already running.');
      const resuming = retry === 'resume';
      const retrying = retry === true;
      if (!queries.length || queries.some(q => !this.ledger.has(queryId(q))
        || (retrying && effectiveStatus(this.ledger.get(queryId(q))) !== 'error')
        || (resuming && finished.has(effectiveStatus(this.ledger.get(queryId(q))))))) {
        throw new Error(resuming ? 'Only pending date pairs can be continued.' : 'Only failed date pairs can be retried.');
      }
      this.active = true;
      this.retrying = retrying;
      this.resuming = resuming;
      this.currentRequest = null;
      if (retrying) this.retryCount += 1;
      if (resuming) this.resumeCount += 1;
      this.passIds = new Set(queries.map(queryId));
      for (const id of this.passIds) {
        const existing = this.ledger.get(id);
        if (existing) this.ledger.set(id, { ...existing, status: 'pending', message: '',
          previous_status: retrying ? effectiveStatus(existing) : undefined,
          previous_message: retrying ? existing.message || existing.previous_message : undefined });
      }
    }
    recordStart(event) {
      const metadata = Object.fromEntries(['request_id', 'region', 'revision', 'started_at', 'query_encoding']
        .filter(key => typeof event[key] === 'string').map(key => [key, event[key]]));
      if (Number.isFinite(event.request_interval_seconds)) metadata.request_interval_seconds = event.request_interval_seconds;
      this.currentRequest = metadata;
      this.requests.push({ ...metadata, kind: this.resuming ? 'continue' : this.retrying ? 'retry' : 'search',
        total: this.passIds.size });
    }
    progress(event) {
      const id = queryId(event);
      if (!this.passIds.has(id)) return;
      const previous = this.ledger.get(id);
      const incoming = event.offer;
      const valid = event.status === 'ok' && incoming && incoming.currency === 'EUR'
        && incoming.price !== null && incoming.price !== '' && Number.isFinite(Number(incoming.price)) && Number(incoming.price) >= 0;
      const quote = valid && (!previous.quote || Number(incoming.price) < Number(previous.quote.price))
        ? { ...incoming } : previous.quote;
      const diagnostics = safeDiagnostics(event.diagnostics);
      const attempts = previous.attempts || [];
      if (finished.has(event.status)) attempts.push({ status: event.status, message: event.message || '',
        request_id: this.currentRequest?.request_id || '', diagnostics });
      this.ledger.set(id, { ...previous, ...event, query_id: id, quote, diagnostics,
        attempts: attempts.slice(-10) });
    }
    mergeRow(row) {
      const previous = this.rows.get(row.departure_date);
      if (!previous || (hasPrice(row) && (!hasPrice(previous) || Number(row.best_price) < Number(previous.best_price)))) {
        this.rows.set(row.departure_date, { ...row });
      }
    }
    finish(interrupted = false) {
      if (interrupted) {
        for (const [id, query] of this.ledger) {
          if (query.status === 'running') this.ledger.set(id, { ...query, status: 'error',
            message: 'Search interrupted during this query; availability is unknown.',
            diagnostics: { category: 'stream_interrupted' },
            attempts: [...(query.attempts || []), { status: 'error', request_id: this.currentRequest?.request_id || '',
              message: 'Stream interrupted before this query finished.', diagnostics: { category: 'stream_interrupted' } }].slice(-10) });
        }
      }
      if (this.active) this.lastFinishedAt = Date.now();
      this.active = false;
    }
    failedQueries() {
      return [...this.ledger.values()].filter(q => effectiveStatus(q) === 'error');
    }
    pendingQueries() {
      return [...this.ledger.values()].filter(q => !finished.has(effectiveStatus(q)));
    }
    diagnosticReport() {
      const fields = ['origin', 'destination', 'target_date', 'date_flex_days', 'one_way', 'min_duration',
        'max_duration', 'adults', 'max_results_per_query', 'min_connections_per_slice',
        'max_connections_per_slice', 'exclude_self_transfers'];
      return { version: 1, exported_at: new Date().toISOString(),
        search: Object.fromEntries(fields.filter(key => key in this.config).map(key => [key, this.config[key]])),
        counts: this.counts(), requests: this.requests,
        queries: [...this.ledger.values()].map(q => ({ query_id: q.query_id,
          departure_date: q.departure_date, return_date: q.return_date, status: effectiveStatus(q),
          message: q.message || q.previous_message || '', attempts: q.attempts || [],
          observed_price: q.quote ? { price: q.quote.price, currency: q.quote.currency,
            searched_at: q.quote.searched_at } : null })) };
    }
    counts() {
      const queries = [...this.ledger.values()];
      return { total: queries.length, completed: queries.filter(q => finished.has(effectiveStatus(q))).length,
        failed: this.failedQueries().length, pending: this.pendingQueries().length, passTotal: this.passIds.size,
        passCompleted: queries.filter(q => this.passIds.has(q.query_id) && finished.has(q.status)).length };
    }
    displayRows() {
      return [...this.rows.values()].sort((a, b) => a.departure_date.localeCompare(b.departure_date)).map(row => {
        const queries = [...this.ledger.values()].filter(q => q.departure_date === row.departure_date);
        const errors = queries.filter(q => effectiveStatus(q) === 'error');
        const pending = queries.filter(q => !finished.has(effectiveStatus(q))).length;
        const priced = hasPrice(row);
        let status = priced ? 'ok' : 'no_offers';
        let message = priced ? 'Flights found' : 'Google returned no matching priced results; availability is not confirmed.';
        if (errors.length || pending) {
          status = priced ? 'partial' : errors.length ? 'error' : 'pending';
          message = `${queries.length - pending} of ${queries.length} date searches checked.`;
          if (errors.length) message += ` ${errors.length} failed. ${errors[0].message || errors[0].previous_message || ''}`;
          if (pending) message += ` ${pending} pending.`;
          message += priced ? ' Best available price may be incomplete.' : ' Price unavailable.';
        }
        return { ...row, search_status: status, search_message: message };
      });
    }
  }

  function calendarData(state) {
    const queries = [...state.ledger.values()];
    const departures = [...new Set(queries.map(q => q.departure_date))].sort();
    const returns = state.config.one_way ? [null] : [...new Set(queries.map(q => q.return_date))].sort();
    const prices = queries.filter(q => q.quote).map(q => Number(q.quote.price));
    const min = prices.length ? Math.min(...prices) : null;
    const max = prices.length ? Math.max(...prices) : null;
    return { departures, returns, min, max, priced: prices.length,
      rows: departures.map(departure => ({ departure, cells: returns.map(returning => {
        const query = state.ledger.get(queryId({ departure_date: departure, return_date: returning }));
        if (!query) return { planned: false, departure, returning };
        const price = query.quote ? Number(query.quote.price) : null;
        const ratio = price === null || min === max ? 0 : (price - min) / (max - min);
        const from = ratio <= 0.5 ? [215, 241, 222] : [255, 244, 194];
        const to = ratio <= 0.5 ? [255, 244, 194] : [250, 216, 210];
        const fraction = ratio <= 0.5 ? ratio * 2 : (ratio - 0.5) * 2;
        const color = price === null ? null : `rgb(${from.map((value, i) => Math.round(value + (to[i] - value) * fraction)).join(', ')})`;
        return { ...query, planned: true, departure, returning, price,
          best: price !== null && price === min, color };
      }) })) };
  }

  async function readStream(response, onEvent) {
    if (!response.body) throw new Error('Streaming is unavailable in this browser.');
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    function consume(line) {
      if (!line.trim()) return false;
      const event = JSON.parse(line);
      onEvent(event);
      if (event.type === 'error') throw new Error(event.error || 'Search failed.');
      return event.type === 'done';
    }
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        for (const line of lines) if (consume(line)) return;
        if (done) {
          if (buffer && consume(buffer)) return;
          throw new Error('Search interrupted before completion. Displayed results may be incomplete.');
        }
      }
    } finally {
      try { await reader.cancel(); } catch (_) { /* Transport may already be closed. */ }
      reader.releaseLock();
    }
  }

  function escapeHtml(value) {
    return String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;')
      .replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#039;');
  }
  function tableClipboard(rows, columns) {
    const clean = value => String(value ?? 'N/A').replace(/[\t\r\n]+/g, ' ');
    const cells = [columns.map(([, label]) => clean(label)),
      ...rows.map(row => columns.map(([key]) => clean(row[key])))];
    const text = cells.map((row, index) => row.map((value, column) => {
      const numeric = index > 0 && columns[column][0] === 'best_price' && hasPrice(rows[index - 1]);
      return !numeric && /^[=+\-@]/.test(value) ? `'${value}` : value;
    }).join('\t')).join('\n');
    const header = cells[0].map(value => `<th>${escapeHtml(value)}</th>`).join('');
    const body = rows.map((row, index) => '<tr>' + columns.map(([key], column) => {
      const numeric = key === 'best_price' && hasPrice(row);
      const attrs = numeric ? ` x:num="${Number(row.best_price)}" style="mso-number-format:'0.00'"`
        : ` style="mso-number-format:'\\@'"`;
      return `<td${attrs}>${escapeHtml(cells[index + 1][column])}</td>`;
    }).join('') + '</tr>').join('');
    return { text, html: `<table xmlns:x="urn:schemas-microsoft-com:office:excel"><thead><tr>${header}</tr></thead><tbody>${body}</tbody></table>` };
  }
  async function writeClipboard(payload, clipboard = root.navigator?.clipboard,
    Item = root.ClipboardItem, BlobType = root.Blob) {
    if (clipboard?.write && Item && BlobType) {
      try {
        await clipboard.write([new Item({ 'text/html': new BlobType([payload.html], { type: 'text/html' }),
          'text/plain': new BlobType([payload.text], { type: 'text/plain' }) })]);
        return;
      } catch (_) { /* Some browsers support only plain text clipboard access. */ }
    }
    if (!clipboard?.writeText) throw new Error('Clipboard access is unavailable.');
    await clipboard.writeText(payload.text);
  }
  root.FlightUI = { addDays, windows, timelineScale, queryPlan, queryId, calendarData,
    SearchState, readStream, tableClipboard, writeClipboard };
  if (typeof module !== 'undefined') module.exports = root.FlightUI;
})(typeof globalThis !== 'undefined' ? globalThis : window);
