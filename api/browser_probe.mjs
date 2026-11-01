import chromium from '@sparticuz/chromium';
import puppeteer from 'puppeteer-core';
import {comparePair, validateUrls} from '../browser-probe.mjs';

export const maxDuration = 180;

export default async function handler(req, res) {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).json({error: 'POST required.'});
  let body;
  try { body = typeof req.body === 'string' ? JSON.parse(req.body) : req.body; }
  catch { return res.status(400).json({error: 'Invalid JSON.'}); }
  if (String(body?.password || '').toLowerCase() !== 'victor') return res.status(401).json({error: 'Incorrect password.'});
  let urls;
  try { urls = validateUrls(body?.urls); }
  catch { return res.status(400).json({error: 'Provide 1–3 English EUR Google Flights search URLs.'}); }
  const metadata = {region: process.env.VERCEL_REGION || 'local', revision: process.env.VERCEL_GIT_COMMIT_SHA || null};
  let browser;
  const results = [];
  try {
    chromium.setGraphicsMode = false;
    browser = await puppeteer.launch({args: puppeteer.defaultArgs({args: chromium.args, headless: 'shell'}),
      executablePath: await chromium.executablePath(), headless: 'shell', defaultViewport: {width: 1365, height: 900}});
    for (const url of urls) {
      const started = Date.now();
      const result = await comparePair(browser, url);
      results.push(result);
      if (['http_error', 'challenge'].includes(result.browser.outcome) || result.http.http_status !== 200) break;
      const remaining = 1500 - (Date.now() - started);
      if (remaining > 0) await new Promise(resolve => setTimeout(resolve, remaining));
    }
    return res.status(200).json({...metadata, results});
  } catch (error) {
    console.error(JSON.stringify({event: 'browser_probe_error', error_type: error.name, ...metadata}));
    return res.status(502).json({...metadata, results, error: browser ? 'Browser comparison failed.' : 'Chromium could not start.', error_type: error.name});
  } finally {
    if (browser) await browser.close().catch(() => {});
  }
}
