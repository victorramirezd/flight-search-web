// This probe returns public fare-card observations only, never HTML or cookies.
export function validateUrls(value) {
  if (!Array.isArray(value) || value.length !== 1) {
    throw new Error('Provide one Google Flights URL.');
  }
  return value.map(text => {
    if (typeof text !== 'string' || text.length > 8192) throw new Error('Invalid search URL.');
    const url = new URL(text);
    if (url.origin !== 'https://www.google.com' || url.username || url.password ||
        !['/travel/flights', '/travel/flights/search'].includes(url.pathname) ||
        !/^[A-Za-z0-9_-]+$/.test(url.searchParams.get('tfs') || '') ||
        [...url.searchParams.keys()].some(key => !['tfs', 'tfu', 'hl', 'curr'].includes(key)) ||
        url.searchParams.get('hl') !== 'en' || url.searchParams.get('curr') !== 'EUR') {
      throw new Error('Only English EUR Google Flights search URLs are accepted.');
    }
    url.hash = '';
    return url.href;
  });
}

export function summarizeHtml(html, status) {
  return {http_status: status, has_results_script: /class=["']ds:1["']/.test(html),
    upstream_error: /errorHasStatus\s*:\s*true/.test(html),
    flight_card_count: (html.match(/class=["']pIav2d["']/g) || []).length};
}

// Self-contained so the same readiness rule runs in Chromium and in unit checks.
export function readFareCards(includeEmpty = false) {
  const challenge = /\/sorry\//.test(location.pathname) || !!document.querySelector('iframe[src*="recaptcha"]');
  const cards = Array.from(document.querySelectorAll('li.pIav2d'));
  const labels = cards.map(card => {
    const text = (card.querySelector('.YMlIz')?.textContent || card.innerText || '').
      replace(/[\u200e\u200f\u202a-\u202e\u2066-\u2069]/g, '').trim();
    return text.match(/€\s*\d[\d,.]*/)?.[0] ||
      (card.innerText || '').match(/€\s*\d[\d,.]*/)?.[0] || '';
  });
  const prices = labels.filter(text => /^€[\s\d,.]+$/.test(text) && /\d/.test(text));
  if (!includeEmpty && !challenge && !prices.length) return null;
  return {outcome: challenge ? 'challenge' : prices.length ? 'priced_cards' : 'no_priced_cards',
    flight_card_count: cards.length, priced_card_count: prices.length, price_labels: prices.slice(0, 20),
    candidate_price_labels: labels.filter(text => /^[€$£\s\d.,]+$/.test(text)).slice(0, 20),
    card_text_samples: !prices.length && includeEmpty ? cards.slice(0, 2).map(card => (card.innerText || '').slice(0, 600)) : [],
    initial_upstream_error: /errorHasStatus\s*:\s*true/.test(document.querySelector('script.ds\\:1')?.textContent || '')};
}

export async function comparePair(browser, url) {
  const started = Date.now();
  const userAgent = await browser.userAgent();
  const httpStart = Date.now();
  const response = await fetch(url, {headers: {'user-agent': userAgent, 'accept-language': 'en-US,en;q=0.9'},
    signal: AbortSignal.timeout(15000), redirect: 'follow'});
  const http = {...summarizeHtml(await response.text(), response.status), elapsed_ms: Date.now() - httpStart};
  // Serverless Chromium uses single-process mode, which cannot create additional
  // contexts reliably. Each probe launches its own browser for exactly one pair.
  const context = browser.defaultBrowserContext();
  const page = await context.newPage();
  let requests = 0, rpcResponses = 0;
  const rpcReads = [];
  page.on('request', () => requests++);
  page.on('response', res => {
    if (res.url().includes('GetShoppingResults') && rpcResponses < 5) {
      rpcResponses++;
      rpcReads.push(res.text().then(text => ({http_status: res.status(), body_characters: text.length,
        provider_error: /ErrorResponse|errorHasStatus\s*:\s*true/.test(text)})).catch(() => ({body_unavailable: true})));
    }
  });
  let consentHandled = false;
  const browserStart = Date.now();
  try {
    const navigation = await page.goto(url, {waitUntil: 'domcontentloaded', timeout: 25000});
    if (navigation && navigation.status() !== 200) {
      return {http, browser: {outcome: 'http_error', http_status: navigation.status(), requests}};
    }
    // Respect the returned reject-optional-cookies form; no saved user sessions.
    if (await page.$('form[action="https://consent.google.com/save"] input[name="set_eom"][value="true"]')) {
      consentHandled = true;
      await Promise.all([page.waitForNavigation({waitUntil: 'domcontentloaded', timeout: 20000}),
        page.evaluate(() => document.querySelector('form[action="https://consent.google.com/save"] input[name="set_eom"][value="true"]').form.submit())]);
    }
    try {
      await page.waitForFunction(readFareCards, {timeout: 20000});
    } catch (error) {
      if (error.name !== 'TimeoutError') throw error;
    }
    const observed = await page.evaluate(readFareCards, true);
    const browserResult = {...observed, consent_handled: consentHandled, requests,
      shopping_responses: rpcResponses, shopping_response_summaries: await Promise.all(rpcReads),
      elapsed_ms: Date.now() - browserStart};
    // A delayed HTTP control helps distinguish elapsed time from browser fetching.
    let delayedHttp = null;
    if (http.upstream_error && observed.outcome !== 'challenge') {
      const delayedStart = Date.now();
      try {
        const delayed = await fetch(url, {headers: {'user-agent': userAgent, 'accept-language': 'en-US,en;q=0.9'},
          signal: AbortSignal.timeout(15000), redirect: 'follow'});
        delayedHttp = {...summarizeHtml(await delayed.text(), delayed.status), elapsed_ms: Date.now() - delayedStart};
      } catch (error) { delayedHttp = {outcome: 'transport_error', error_type: error.name}; }
    }
    return {http, browser: browserResult, delayed_http: delayedHttp,
      http_get_requests: delayedHttp ? 2 : 1, elapsed_ms: Date.now() - started};
  } finally {
    await page.close();
  }
}
