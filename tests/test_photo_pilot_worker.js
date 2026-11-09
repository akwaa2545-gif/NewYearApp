const { test } = require('node:test');
const assert = require('node:assert/strict');
const { createHash } = require('node:crypto');

const moduleReady = import('../photo_api/pilot_worker.mjs');
const IDS = ['1001519', '1001801', '1003585', '1003603', '1003615',
  '1003657', '1003732', '1003806', '1003865', '1003956'];
// Deliberately fake fixtures; deployment must use independent generated secrets.
const EDGE_TOKEN = 'test-only-edge-0123456789-ABCDEFGHIJKLMN';
const API_TOKEN = 'test-only-backend-9876543210-ZYXWVUTSRQ';
const JPEG = Uint8Array.from([255, 216, 255, 224, 0, 2, 255, 217]);
const PHOTO = `/v1/employees/${IDS[0]}/photo`;
function environment(fetcher = async () => new Response(JPEG, {
  headers: { 'Content-Type': 'image/jpeg' },
})) {
  return { PHOTO_API: { fetch: fetcher }, PHOTO_API_TOKEN: API_TOKEN,
    PILOT_ACCESS_TOKEN: EDGE_TOKEN, PILOT_EMPLOYEE_IDS: JSON.stringify(IDS) };
}
async function request(path = PHOTO, { method = 'GET', token = EDGE_TOKEN, headers = {} } = {}, env = environment()) {
  const authorization = token === null ? {} : { Authorization: `Bearer ${token}` };
  return (await moduleReady).default.fetch(new Request(`https://private-pilot.example${path}`, {
    method, headers: { ...authorization, ...headers },
  }), env);
}
function safeHeaders(response) {
  assert.match(response.headers.get('Cache-Control'), /no-store/);
  assert.equal(response.headers.get('X-Content-Type-Options'), 'nosniff');
  assert.equal(response.headers.get('Vary'), 'Authorization');
  for (const key of ['Access-Control-Allow-Origin', 'Set-Cookie', 'Server', 'Location']) {
    assert.equal(response.headers.get(key), null);
  }
}
function streamed(chunks, { type = 'image/jpeg', length } = {}) {
  let cancelled = false;
  const pending = [...chunks];
  const body = new ReadableStream({
    pull(controller) { pending.length ? controller.enqueue(pending.shift()) : controller.close(); },
    cancel() { cancelled = true; },
  });
  return { response: new Response(body, { headers: {
    'Content-Type': type, ...(length === undefined ? {} : { 'Content-Length': String(length) }),
  } }), cancelled: () => cancelled };
}

test('private pilot serves only validated JPEG bytes through fixed VPC service request', async () => {
  let seen;
  const response = await request(PHOTO, { headers: {
    Cookie: 'secret-cookie', Host: 'attacker.example', 'X-Forwarded-Host': 'attacker.example',
    Origin: 'https://attacker.example', 'If-None-Match': '*',
  } }, environment(async upstream => {
    seen = upstream;
    return new Response(JPEG, { headers: {
      'Content-Type': 'image/jpeg', Server: 'private-sql-server', 'Set-Cookie': 'private=value',
      'Access-Control-Allow-Origin': '*', ETag: 'private-identifier',
    } });
  }));
  assert.equal(response.status, 200);
  assert.equal(seen.url, `http://127.0.0.1:8765${PHOTO}`);
  assert.equal(seen.method, 'GET');
  assert.equal(seen.redirect, 'manual');
  assert.equal(seen.headers.get('Authorization'), `Bearer ${API_TOKEN}`);
  assert.equal(seen.headers.get('Accept'), 'image/jpeg');
  assert.deepEqual([...seen.headers.keys()].sort(), ['accept', 'authorization']);
  const bytes = new Uint8Array(await response.arrayBuffer());
  assert.deepEqual(bytes, JPEG);
  assert.equal(createHash('sha256').update(bytes).digest('hex'), createHash('sha256').update(JPEG).digest('hex'));
  assert.equal(response.headers.get('ETag'), null);
  assert.equal(response.headers.get('Content-Length'), String(JPEG.length));
  safeHeaders(response);
});

test('all paths require edge bearer authorization before any backend request', async () => {
  let calls = 0;
  const env = environment(async () => { calls++; throw new Error('should never call'); });
  for (const path of [PHOTO, '/healthz', '/readyz', '/', '/unknown']) {
    for (const token of [null, '', API_TOKEN, 'wrong', EDGE_TOKEN + 'x']) {
      const response = await request(path, { token }, env);
      assert.equal(response.status, 401);
      safeHeaders(response);
      assert.equal(response.headers.get('WWW-Authenticate'), 'Bearer');
    }
  }
  assert.equal(calls, 0);
});

test('configuration fails closed for missing bindings, weak/reused secrets or invalid ten-ID allowlist', async () => {
  let calls = 0;
  const base = environment(async () => { calls++; throw new Error('unexpected'); });
  const overrides = [{ PHOTO_API: undefined }, { PHOTO_API: {} }, { PHOTO_API_TOKEN: undefined },
    { PHOTO_API_TOKEN: 'short' }, { PILOT_ACCESS_TOKEN: 'x'.repeat(40) },
    { PILOT_ACCESS_TOKEN: API_TOKEN }, { PILOT_ACCESS_TOKEN: 'a'.repeat(31) },
    { PILOT_ACCESS_TOKEN: EDGE_TOKEN + '\n' }, { PILOT_ACCESS_TOKEN: 'a'.repeat(513) },
    { PILOT_EMPLOYEE_IDS: undefined }, { PILOT_EMPLOYEE_IDS: 'broken-json' },
    { PILOT_EMPLOYEE_IDS: '{}' }, { PILOT_EMPLOYEE_IDS: JSON.stringify(IDS.slice(1)) },
    { PILOT_EMPLOYEE_IDS: JSON.stringify([...IDS, '1000000']) },
    { PILOT_EMPLOYEE_IDS: JSON.stringify([...IDS.slice(1), IDS[1]]) },
    { PILOT_EMPLOYEE_IDS: JSON.stringify([1001519, ...IDS.slice(1)]) },
    { PILOT_EMPLOYEE_IDS: JSON.stringify(['123456', ...IDS.slice(1)]) }];
  for (const override of overrides) {
    const response = await request(PHOTO, {}, { ...base, ...override });
    assert.equal(response.status, 503, JSON.stringify(override));
    assert.equal(await response.text(), '{"error":"unavailable"}\n');
    safeHeaders(response);
  }
  assert.equal(calls, 0);
});

test('methods, malformed IDs, unknown paths, query strings and nonpilot IDs never reach backend', async () => {
  let calls = 0;
  const env = environment(async () => { calls++; throw new Error('unexpected'); });
  for (const method of ['POST', 'PUT', 'DELETE', 'OPTIONS', 'PATCH']) {
    const response = await request(PHOTO, { method }, env);
    assert.equal(response.status, 405);
    assert.equal(response.headers.get('Allow'), 'GET, HEAD');
  }
  for (const path of ['/v1/employees/123/photo', '/v1/employees/abcdefg/photo',
    '/v1/employees/%31' + IDS[0].slice(1) + '/photo', '/v1/employees//photo',
    PHOTO + '?url=https://attacker.example', '/healthz?']) {
    assert.equal((await request(path, {}, env)).status, 400, path);
  }
  for (const path of ['/', '/employees', '/v1/employees', '/favicon.ico', '/v1/employees/9999999/photo']) {
    assert.equal((await request(path, {}, env)).status, 404, path);
  }
  assert.equal(calls, 0);
});

test('each of the configured ten IDs is supported', async () => {
  for (const id of IDS) assert.equal((await request(`/v1/employees/${id}/photo`)).status, 200);
});

test('explicit all-employees scope serves canonical IDs beyond the legacy pilot', async () => {
  let seen;
  const env = { ...environment(async upstream => {
    seen = upstream;
    return new Response(JPEG, { headers: { 'Content-Type': 'image/jpeg' } });
  }), PHOTO_SCOPE: 'all-employees', PILOT_EMPLOYEE_IDS: undefined };
  for (const id of ['0001600', '2412577']) {
    const response = await request(`/v1/employees/${id}/photo`, {}, env);
    assert.equal(response.status, 200);
    assert.equal(seen.url, `http://127.0.0.1:8765/v1/employees/${id}/photo`);
    assert.equal(seen.headers.get('Authorization'), `Bearer ${API_TOKEN}`);
    safeHeaders(response);
  }
});

test('scope is fail-closed and explicit pilot mode retains the ten-ID guard', async () => {
  let calls = 0;
  const env = environment(async () => { calls++; throw new Error('unexpected'); });
  for (const scope of ['', null, false, {}, 'all', 'ALL-EMPLOYEES', 'all-employees ']) {
    assert.equal((await request(PHOTO, {}, { ...env, PHOTO_SCOPE: scope })).status, 503);
  }
  assert.equal((await request('/v1/employees/2412577/photo', {}, { ...env, PHOTO_SCOPE: 'pilot' })).status, 404);
  assert.equal(calls, 0);
});

test('all-employees scope still requires private bearer and canonical safe paths', async () => {
  let calls = 0;
  const env = { ...environment(async () => { calls++; throw new Error('unexpected'); }), PHOTO_SCOPE: 'all-employees' };
  for (const token of [null, '', 'wrong', API_TOKEN]) {
    assert.equal((await request('/v1/employees/2412577/photo', { token }, env)).status, 401);
  }
  for (const path of ['/v1/employees/123/photo', '/v1/employees/abcdefg/photo',
    '/v1/employees/%32001519/photo', '/v1/employees/2412577/photo?x=1']) {
    assert.equal((await request(path, {}, env)).status, 400);
  }
  assert.equal(calls, 0);
});

test('all-employees scope returns SQL missing-photo status without leaking backend payload', async () => {
  const env = { ...environment(async () => new Response('private SQL details', { status: 404 })),
    PHOTO_SCOPE: 'all-employees', PILOT_EMPLOYEE_IDS: undefined };
  const response = await request('/v1/employees/0305528/photo', {}, env);
  assert.equal(response.status, 404);
  assert.equal(await response.text(), '{"error":"not found"}\n');
  safeHeaders(response);
});

test('HEAD forwards HEAD, validates bounded length and JPEG type, always returns no body', async () => {
  let method;
  const env = environment(async upstream => {
    method = upstream.method;
    return new Response(null, { headers: { 'Content-Type': 'image/jpeg', 'Content-Length': '83' } });
  });
  const response = await request(PHOTO, { method: 'HEAD' }, env);
  assert.equal(response.status, 200);
  assert.equal(method, 'HEAD');
  assert.equal(response.body, null);
  assert.equal(response.headers.get('Content-Length'), '83');
  for (const length of [undefined, '-1', '0', '2', '262145', '1.5', '4e1', 'x']) {
    const headers = { 'Content-Type': 'image/jpeg', ...(length === undefined ? {} : { 'Content-Length': length }) };
    const bad = await request(PHOTO, { method: 'HEAD' }, environment(async () => new Response(null, { headers })));
    assert.equal(bad.status, 503, String(length));
    assert.equal(bad.body, null);
  }
  const rejected = await request(PHOTO, { method: 'HEAD', token: null });
  assert.equal(rejected.status, 401);
  assert.equal(rejected.body, null);
});

test('streamed JPEG length is bounded using actual bytes and signature across chunks', async () => {
  const split = streamed([JPEG.slice(0, 1), JPEG.slice(1, 2), JPEG.slice(2)], { length: 1 });
  const good = await request(PHOTO, {}, environment(async () => split.response));
  assert.deepEqual(new Uint8Array(await good.arrayBuffer()), JPEG);
  const max = new Uint8Array(256 * 1024);
  max.set(JPEG);
  assert.equal((await request(PHOTO, {}, environment(async () => streamed([max]).response))).status, 200);
  const tooLarge = streamed([JPEG, new Uint8Array(256 * 1024), new Uint8Array(10)], { length: 8 });
  assert.equal((await request(PHOTO, {}, environment(async () => tooLarge.response))).status, 503);
  assert.equal(tooLarge.cancelled(), true);
  for (const bytes of [new Uint8Array(), Uint8Array.of(255, 216), Uint8Array.of(0, 216, 255)]) {
    assert.equal((await request(PHOTO, {}, environment(async () => new Response(bytes,
      { headers: { 'Content-Type': 'image/jpeg' } })))).status, 503);
  }
  for (const type of ['image/png', 'image/jpeg; charset=utf-8', 'IMAGE/JPEG', 'text/html']) {
    assert.equal((await request(PHOTO, {}, environment(async () => new Response(JPEG,
      { headers: { 'Content-Type': type } })))).status, 503);
  }
});

test('health endpoints expose generated JSON and accept only validated bounded backend status', async () => {
  for (const [path, text, status] of [['/healthz', 'ok\n', 'ok'], ['/readyz', 'ready\n', 'ready']]) {
    for (const body of [text, JSON.stringify({ status, unexpected: 'private-server-name' })]) {
      const type = body === text ? 'text/plain; charset=utf-8' : 'application/json';
      const response = await request(path, {}, environment(async () => new Response(body, {
        headers: { 'Content-Type': type },
      })));
      assert.equal(response.status, 200);
      assert.deepEqual(await response.json(), { status });
      safeHeaders(response);
    }
    const head = await request(path, { method: 'HEAD' }, environment(async () => new Response(null, {
      headers: { 'Content-Type': 'text/plain; charset=utf-8', 'Content-Length': String(text.length) },
    })));
    assert.equal(head.status, 200);
    assert.equal(head.body, null);
  }
  for (const body of ['<html>private-server-name</html>', '{"status":"wrong"}', 'broken-json', 'x'.repeat(4097)]) {
    const response = await request('/readyz', {}, environment(async () => streamed(
      [new TextEncoder().encode(body)], { type: 'application/json', length: 1 }).response));
    assert.equal(response.status, 503);
    assert.equal(await response.text(), '{"error":"unavailable"}\n');
  }
});

test('backend denied, redirects, SQL failure, missing photo and unknown statuses are sanitized', async () => {
  for (const status of [201, 204, 301, 302, 307, 308, 400, 401, 403, 404, 418, 429, 500, 503]) {
    const response = await request(PHOTO, {}, environment(async () => new Response(status === 204 ? null :
      `private-sql-server ${API_TOKEN}`, { status, headers: { Location: 'https://attacker.example', Server: 'sql' } })));
    assert.equal(response.status, status === 404 ? 404 : 503);
    const body = await response.text();
    assert.equal(body.includes(API_TOKEN), false);
    assert.equal(body.includes('private-sql-server'), false);
    safeHeaders(response);
  }
  const response = await request('/readyz', {}, environment(async () => { throw new Error(`${API_TOKEN} sql-private stack`); }));
  assert.equal(response.status, 503);
  assert.equal(await response.text(), '{"error":"unavailable"}\n');
  assert.equal((await request('/readyz', {}, environment(async () => new Response('missing', { status: 404 })))).status, 503);
});

test('eight second deadline aborts backend fetch and slow response streams', async t => {
  for (const slowStream of [false, true]) {
    t.mock.timers.enable({ apis: ['setTimeout'] });
    let started;
    let signal;
    let cancelled = false;
    const called = new Promise(resolve => { started = resolve; });
    const env = environment(async upstream => {
      signal = upstream.signal;
      if (!slowStream) { started(); return new Promise(() => {}); }
      return new Response(new ReadableStream({
        pull(controller) { controller.enqueue(JPEG); started(); return new Promise(() => {}); },
        cancel() { cancelled = true; },
      }), { headers: { 'Content-Type': 'image/jpeg' } });
    });
    const pending = request(PHOTO, {}, env);
    await called;
    t.mock.timers.tick(8000);
    const response = await pending;
    assert.equal(response.status, 503);
    assert.equal(signal.aborted, true);
    assert.equal(await response.text(), '{"error":"unavailable"}\n');
    if (slowStream) assert.equal(cancelled, true);
    t.mock.timers.reset();
  }
});
