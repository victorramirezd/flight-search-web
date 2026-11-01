import assert from 'node:assert/strict';
import {validateUrls, summarizeHtml, comparePair, readFareCards} from '../browser-probe.mjs';
import handler from '../api/browser_probe.mjs';
import chromium from '@sparticuz/chromium';
import puppeteer from 'puppeteer-core';

const url = 'https://www.google.com/travel/flights/search?tfs=ABC_123&hl=en&curr=EUR';
assert.deepEqual(validateUrls([url]), [url]);
for (const bad of [[], [url,url], null, ['http://www.google.com/travel/flights?tfs=ABC&hl=en&curr=EUR'],
  [url.replace('www.google.com', 'evil.example')], [url.replace('/travel/flights/search', '/url')],
  [url+'&q=https://evil.example'], [url.replace('ABC_123','https://evil.example')],
  [url.replace('www.google.com','user:password@www.google.com')]]) {
  assert.throws(() => validateUrls(bad));
}
assert.deepEqual(summarizeHtml('<script class="ds:1">SECRET errorHasStatus: true</script>',200),
  {http_status:200,has_results_script:true,upstream_error:true,flight_card_count:0});
assert.equal(JSON.stringify(summarizeHtml('SECRET_COOKIE_AND_HTML',200)).includes('SECRET'),false);

globalThis.location={pathname:'/travel/flights'};
globalThis.document={querySelector(){return null;},querySelectorAll(){return [{querySelector(){return {textContent:''};}}];}};
assert.equal(readFareCards(),null,'empty price elements are not completed results');
globalThis.document.querySelectorAll=()=>[{querySelector(){return {textContent:'\u200e€147'};}}];
assert.deepEqual(readFareCards().price_labels,['€147']);
globalThis.document.querySelectorAll=()=>[{querySelector(){return {textContent:'251'};},innerText:'Turkish Airlines\n€251\nround trip'}];
assert.deepEqual(readFareCards().price_labels,['€251'],'visible card text remains readable when currency markup changes');
delete globalThis.location;delete globalThis.document;

const originalFetch = globalThis.fetch;
globalThis.fetch = async () => ({status:200,text:async()=>'<script class="ds:1">errorHasStatus: true</script>'});
try {
  for (const shouldFail of [false,true]) {
    let closed = 0;
    const page = {on(){}, async goto(){return {status:()=>200};}, async $(){return null;},
      async waitForFunction(){if(shouldFail) throw new Error('navigation failed');},
      async evaluate(){return {outcome:'priced_cards',priced_card_count:1,price_labels:['€147']};},
      async close(){closed++;}};
    const browser = {async userAgent(){return 'test-browser';}, defaultBrowserContext(){return {
      async newPage(){return page;}};}};
    if(shouldFail) await assert.rejects(comparePair(browser,url),/navigation failed/);
    else {
      const result = await comparePair(browser,url);
      assert.equal(result.http.upstream_error,true);
      assert.equal(result.browser.outcome,'priced_cards');
    }
    assert.equal(closed,1,'the per-query page must close on success and failure');
  }
} finally { globalThis.fetch = originalFetch; }
// Exercise the actual installed Puppeteer API without launching a local browser.
const originalLaunch = puppeteer.launch;
const originalPath = chromium.executablePath;
let launchOptions;
try {
  chromium.executablePath = async () => '/test/chromium';
  puppeteer.launch = async options => { launchOptions=options; throw new Error('controlled launch failure'); };
  const res = {setHeader(){},status(){return this;},json(){}};
  const originalError = console.error;
  try {
    console.error=()=>{};
    await handler({method:'POST',body:{password:'victor',urls:[url]}},res);
  } finally {console.error=originalError;}
  assert.ok(Array.isArray(launchOptions.args),'resolve asynchronous defaultArgs before launching');
  assert.equal(launchOptions.executablePath,'/test/chromium');
} finally {puppeteer.launch=originalLaunch;chromium.executablePath=originalPath;}
console.log('Browser probe URL boundaries, safe output and context cleanup checks passed');
