const {test} = require('node:test');
const assert = require('node:assert/strict');

const newOrigin = 'https://tokinparty27.pages.dev';
const canonical = 'https://eventdesk.pages.dev';
const loadEntry = async () => (await import('../cloudflare/tokinparty27-pages.mjs')).default;

test('the new Pages address forwards API and asset reads to the existing canonical Worker', async () => {
  const entry = await loadEntry();
  const binding = {EVENTDESK:{fetch(request) { return new Response(request.url); }}};
  for (const path of ['/api/me?backup=1', '/projector', '/static/app.js', '//elsewhere.invalid/api/me']) {
    const response = await entry.fetch(new Request(newOrigin + path), binding);
    assert.equal(await response.text(), canonical + path);
  }
});

test('same-origin POSTs keep their body, session, CSRF and operation identity', async () => {
  const entry = await loadEntry();
  const body = '{"ids":[2,1],"expected_ids":[1,2]}';
  const request = new Request(newOrigin + '/api/rewards/reorder', {method:'POST', body, headers:{
    Origin:newOrigin, 'Sec-Fetch-Site':'same-origin', Cookie:'party_session=fixture',
    'X-CSRF-Token':'fixture-csrf', 'X-EventDesk-Operation-Id':'fixture-id'
  }});
  const response = await entry.fetch(request, {EVENTDESK:{async fetch(forwarded) {
    assert.equal(forwarded.url, canonical + '/api/rewards/reorder');
    assert.equal(forwarded.method, 'POST');
    for (const [name, value] of request.headers) {
      assert.equal(forwarded.headers.get(name), name === 'origin' ? canonical : value);
    }
    assert.equal(await forwarded.text(), body);
    return new Response('saved', {headers:{'Set-Cookie':'party_session=fixture; Secure; HttpOnly; Path=/'}});
  }}});
  assert.equal(response.headers.get('set-cookie'), 'party_session=fixture; Secure; HttpOnly; Path=/');
});

test('foreign sites, previews and deployment paths cannot use the service binding', async () => {
  const entry = await loadEntry();
  const binding = {EVENTDESK:{fetch() { assert.fail('Blocked requests must not reach the backing Worker'); }}};
  const cases = [
    [new Request(newOrigin + '/api/login', {method:'POST', headers:{Origin:canonical}}), 403],
    [new Request(newOrigin + '/api/staff-session', {method:'POST', headers:{'Sec-Fetch-Site':'cross-site'}}), 403],
    [new Request('https://preview.tokinparty27.pages.dev/api/me'), 421],
    [new Request(newOrigin + '/__deployment/import', {method:'POST'}), 404]
  ];
  for (const [request, status] of cases) assert.equal((await entry.fetch(request, binding)).status, status);
});
