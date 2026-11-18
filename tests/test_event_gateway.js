const { test } = require('node:test');
const assert = require('node:assert/strict');
const { randomBytes } = require('node:crypto');
const modules = Promise.all([import('../photo_api/pilot_worker.mjs'), import('../photo_api/event_worker_proxy.mjs')]);
const ACCESS = randomBytes(32).toString('hex') + '-fixture-alphabet';
const BACKEND = randomBytes(32).toString('hex') + '-fixture-alphabet';
const COMMAND = JSON.stringify({ schema_version: 1, method: 'GET', path: '/api/me' });
const RESULT = JSON.stringify({ schema_version: 1, status: 200, body_base64: 'e30=' });
const encoder = new TextEncoder();
function env(fetcher) { return { PHOTO_SCOPE: 'all-employees', PHOTO_API_TOKEN: BACKEND, PILOT_ACCESS_TOKEN: ACCESS, PHOTO_API: { fetch: fetcher } }; }
function input(path = '/v1/event/request', options = {}) {
  return new Request(`https://photo-pilot.example${path}`, { method: 'POST', body: COMMAND,
    headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': 'application/json' }, ...options, duplex: 'half' });
}
function upstream(body = RESULT, headers = {}) { return new Response(body, { headers: { 'Content-Type': 'application/json; charset=utf-8', ...headers } }); }
async function gateway(request, environment) { return (await modules)[0].default.fetch(request, environment); }
function safe(response) {
  assert.equal(response.headers.get('Cache-Control'), 'private, no-store');
  assert.equal(response.headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(response.headers.get('Vary'), 'Authorization');
  for (const name of ['Set-Cookie', 'Location', 'Server', 'Access-Control-Allow-Origin']) assert.equal(response.headers.get(name), null);
}

test('event routes use the same authenticated VPC service and exact backend bytes', async () => {
  for (const path of ['/v1/event/request', '/v1/event/snapshot']) {
    let seen;
    const response = await gateway(input(path, { headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': 'application/json',
      Cookie: 'private-session', Host: 'attacker.example', Traceparent: 'private-trace', 'X-Forwarded-Host': 'evil' } }), env(async request => {
      seen = request; return upstream(RESULT, { Server: 'private', 'Set-Cookie': 'private', Location: 'https://evil', 'Access-Control-Allow-Origin': '*' });
    }));
    assert.equal(response.status, 200);
    assert.equal(await response.text(), RESULT);
    assert.equal(seen.url, `http://127.0.0.1:8765${path}`);
    assert.equal(seen.method, 'POST'); assert.equal(seen.redirect, 'manual');
    assert.equal(seen.headers.get('Authorization'), `Bearer ${BACKEND}`);
    assert.deepEqual([...seen.headers.keys()].sort(), ['accept', 'authorization', 'content-type']);
    assert.equal(await seen.text(), COMMAND);
    safe(response);
  }
});

test('event routes fail closed on missing access or invalid configuration before any backend fetch', async () => {
  let calls = 0; const environment = env(async () => { calls++; return upstream(); });
  for (const authorization of ['', 'Bearer wrong', `Bearer ${BACKEND}`]) {
    const response = await gateway(input(undefined, { headers: { Authorization: authorization, 'Content-Type': 'application/json' } }), environment);
    assert.equal(response.status, 401); safe(response);
  }
  for (const override of [{ PHOTO_API: null }, { PHOTO_API_TOKEN: 'weak' }, { PHOTO_SCOPE: 'invalid' }])
    assert.equal((await gateway(input(), { ...environment, ...override })).status, 401);
  assert.equal(calls, 0);
});

test('event method, media, encoding, query and empty-body checks happen before backend I/O', async () => {
  let calls = 0; const environment = env(async () => { calls++; return upstream(); });
  for (const method of ['GET', 'HEAD', 'PUT', 'DELETE']) {
    const response = await gateway(input(undefined, { method, body: method === 'GET' || method === 'HEAD' ? undefined : COMMAND }), environment);
    assert.equal(response.status, 405); assert.equal(response.headers.get('Allow'), 'POST');
  }
  assert.equal((await gateway(input('/v1/event/request?url=https://evil'), environment)).status, 400);
  for (const media of ['text/plain', 'application/jsonp', 'application/json; charset=utf-16'])
    assert.equal((await gateway(input(undefined, { headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': media } }), environment)).status, 400);
  assert.equal((await gateway(input(undefined, { headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': 'application/json', 'Content-Encoding': 'gzip' } }), environment)).status, 400);
  assert.equal((await gateway(input(undefined, { body: '' }), environment)).status, 400);
  assert.equal(calls, 0);
});

test('gateway keeps JSON opaque so the backend and primary worker each enforce their own protocol once', async () => {
  for (const body of ['broken-json', '{}', '[]', '{"schema_version":2}']) {
    let received;
    const response = await gateway(input(undefined, { body }), env(async request => {
      received = await request.text(); return new Response('private validation details', { status: 400 });
    }));
    assert.equal(received, body); assert.equal(response.status, 400);
    assert.deepEqual(await response.json(), { error: 'bad request' });
  }
  // The primary Worker must reject these 200 payloads as protocol errors, not
  // outages. The gateway must not mask them as 503 and trigger SQLite fallback.
  for (const body of ['not-json', '[]', '{"schema_version":2}']) {
    const response = await gateway(input(), env(async () => upstream(body)));
    assert.equal(response.status, 200); assert.equal(await response.text(), body);
  }
});

test('request byte cap handles declared and streamed overflow without reaching backend', async () => {
  const helper = (await modules)[1]; let calls = 0;
  assert.equal(helper.EVENT_LIMITS.maxRequestBytes, 12 * 1024 * 1024);
  assert.equal(helper.EVENT_LIMITS.maxResponseBytes, 24 * 1024 * 1024);
  const environment = env(async () => { calls++; return upstream(); });
  const length = helper.EVENT_LIMITS.maxRequestBytes + 1;
  assert.equal((await gateway(input(undefined, { headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': 'application/json', 'Content-Length': String(length) } }), environment)).status, 413);
  let cancelled = false;
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(length)); }, cancel() { cancelled = true; } });
  assert.equal((await gateway(input(undefined, { body: stream }), environment)).status, 413);
  assert.equal(cancelled, true); assert.equal(calls, 0);
  for (const declared of ['invalid', '-1', '1', String(COMMAND.length + 1)]) {
    const response = await gateway(input(undefined, { headers: { Authorization: `Bearer ${ACCESS}`, 'Content-Type': 'application/json', 'Content-Length': declared } }), environment);
    assert.equal(response.status, 400);
  }
});

test('non-200 backend errors retain fail-closed status and never expose private bodies', async () => {
  for (const status of [201, 400, 401, 403, 404, 405, 409, 413, 415, 418, 500, 501, 503, 302]) {
    const response = await gateway(input(), env(async () => new Response(`private ${BACKEND}`, { status, headers: { Location: 'https://evil' } })));
    assert.equal(response.status, [400, 401, 403, 404, 405, 409, 413, 415].includes(status) ? status : status >= 500 ? 503 : 400);
    assert.equal((await response.text()).includes(BACKEND), false); safe(response);
  }
});

test('upstream media, compression, UTF8 and truncated body failures are sanitized', async () => {
  for (const headers of [{ 'Content-Type': 'text/html' }, { 'Content-Type': 'application/json; charset=utf-16' },
    { 'Content-Encoding': 'gzip' }, { 'Content-Length': String(RESULT.length + 1) }, { 'Content-Length': 'garbage' }]) {
    const response = await gateway(input(), env(async () => upstream(RESULT, headers)));
    assert.equal(response.status, 400); assert.deepEqual(await response.json(), { error: 'bad request' });
  }
  for (const body of ['', Uint8Array.of(255, 255), Uint8Array.of(0xe0, 0xa0)])
    assert.equal((await gateway(input(), env(async () => upstream(body)))).status, 400);
});

test('upstream streamed byte overflow is cancelled even when announced size lies', async () => {
  const limit = (await modules)[1].EVENT_LIMITS.maxResponseBytes;
  let cancelled = false;
  const stream = new ReadableStream({ start(controller) { controller.enqueue(new Uint8Array(limit + 1)); }, cancel() { cancelled = true; } });
  const response = await gateway(input(), env(async () => upstream(stream, { 'Content-Length': '1' })));
  assert.equal(response.status, 400); assert.equal(cancelled, true); safe(response);
});

test('total deadline aborts stalled upload, backend fetch and backend response', async () => {
  const { proxyEventRequest } = (await modules)[1];
  for (const stage of ['upload', 'fetch', 'response']) {
    let signal, cancelled = false;
    const slow = () => new ReadableStream({ pull() { return new Promise(() => {}); }, cancel() { cancelled = true; } });
    const request = input(undefined, stage === 'upload' ? { body: slow() } : {});
    const response = await proxyEventRequest(request, { backendToken: BACKEND, service: { fetch: async request => {
      signal = request.signal;
      return stage === 'fetch' ? new Promise(() => {}) : upstream(slow());
    } } }, { deadlineMs: 15 });
    assert.equal(response.status, 503); safe(response);
    if (stage !== 'upload') assert.equal(signal.aborted, true);
    if (stage !== 'fetch') assert.equal(cancelled, true);
  }
});

test('network and body I/O failures remain outages rather than protocol corruption', async () => {
  for (const fetcher of [async () => { throw new Error(`private ${BACKEND}`); }, async () => upstream(new ReadableStream({
    pull(controller) { controller.error(new Error('network disconnected')); },
  }))]) {
    const response = await gateway(input(), env(fetcher));
    assert.equal(response.status, 503); assert.deepEqual(await response.json(), { error: 'unavailable' }); safe(response);
  }
});

test('completed operation clears deadline and succeeds with chunked UTF8 JSON envelopes', async () => {
  const { proxyEventRequest } = (await modules)[1];
  const bytes = encoder.encode(JSON.stringify({ schema_version: 1, name: '\u0e17\u0e14\u0e2a\u0e2d\u0e1a' }));
  const body = new ReadableStream({ start(controller) { controller.enqueue(bytes.subarray(0, 8)); controller.enqueue(bytes.subarray(8)); controller.close(); } });
  const response = await proxyEventRequest(input(), { backendToken: BACKEND, service: { fetch: async () => upstream(body) } }, { deadlineMs: 100 });
  assert.equal(response.status, 200); assert.equal((await response.json()).name, '\u0e17\u0e14\u0e2a\u0e2d\u0e1a'); safe(response);
});
