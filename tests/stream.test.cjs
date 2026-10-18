const assert = require('node:assert/strict');
const { readStream } = require('../search-ui.js');

function response(chunks) {
  let index = 0;
  const reader = { cancelled: false, released: false,
    async read() { return index < chunks.length ? { value: chunks[index++], done: false } : { done: true }; },
    async cancel() { this.cancelled = true; }, releaseLock() { this.released = true; } };
  return { body: { getReader() { return reader; } }, reader };
}
const encode = text => new TextEncoder().encode(text);

(async () => {
  const bytes = encode('{"type":"start"}\n{"type":"progress","status":"running"}\n{"type":"row","row":{"airlines":"Aéro"}}\n{"type":"done"}');
  const accent = bytes.indexOf(195);
  const res = response([bytes.slice(0, accent + 1), bytes.slice(accent + 1)]);
  const events = [];
  await readStream(res, event => events.push(event));
  assert.deepEqual(events.map(e => e.type), ['start', 'progress', 'row', 'done']);
  assert.equal(events[2].row.airlines, 'Aéro');
  assert.equal(res.reader.cancelled, true);
  assert.equal(res.reader.released, true);
  await assert.rejects(readStream(response([encode('{"type":"start"}\n')]), () => {}), /interrupted before completion/);
  const failure = response([encode('{"type":"error","error":"Google blocked this search"}\n')]);
  await assert.rejects(readStream(failure, () => {}), /Google blocked this search/);
  assert.equal(failure.reader.released, true);
  await assert.rejects(readStream(response([encode('invalid json\n')]), () => {}), SyntaxError);
  console.log('Stream progress, split UTF-8, terminal errors, interruption and reader cleanup checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
