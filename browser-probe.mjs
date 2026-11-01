// This probe returns public fare-card observations only, never HTML or cookies.
export function validateUrls(value) {
  if (!Array.isArray(value) || value.length < 1 || value.length > 3) {
    throw new Error('Provide one to three Google Flights URLs.');
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

export async function comparePair(browser, url) {
  const started = Date.now();
  const userAgent = await browser.userAgent();
  const httpStart = Date.now();
  const response = await fetch(url, {headers: {'user-agent': userAgent, 'accept-language': 'en-US,en;q=0.9'},
    signal: AbortSignal.timeout(15000), redirect: 'follow'});
  const http = {...summarizeHtml(await response.text(), response.status), elapsed_ms: Date.now() - httpStart};
  const context = await browser.createBrowserContext();
  const page = await context.newPage();
  let requests = 0, rpcResponses = 0;
  page.on('request', () => requests++);
  page.on('response', res => { if (res.url().includes('GetShoppingResults')) rpcResponses++; });
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
      await page.waitForFunction(() => document.querySelector('li.pIav2d .YMlIz') ||
        /\/sorry\//.test(location.pathname) || document.querySelector('iframe[src*="recaptcha"]'),
        {timeout: 20000});
    } catch (error) {
      if (error.name !== 'TimeoutError') throw error;
    }
    const observed = await page.evaluate(() => {
      const challenge = /\/sorry\//.test(location.pathname) || !!document.querySelector('iframe[src*="recaptcha"]');
      const prices = Array.from(document.querySelectorAll('li.pIav2d')).map(card =>
        card.querySelector('.YMlIz')?.textContent?.trim()).filter(text => /^€[\s\d,.]+$/.test(text || ''));
      return {outcome: challenge ? 'challenge' : prices.length ? 'priced_cards' : 'no_priced_cards',
        priced_card_count: prices.length, price_labels: prices.slice(0, 20),
        initial_upstream_error: /errorHasStatus\s*:\s*true/.test(document.querySelector('script.ds\\:1')?.textContent || '')};
    });
    return {http, browser: {...observed, consent_handled: consentHandled, requests,
      shopping_responses: rpcResponses, elapsed_ms: Date.now() - browserStart}, elapsed_ms: Date.now() - started};
  } finally {
    await context.close();
  }
}
