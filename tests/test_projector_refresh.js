const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function fixture({includeStorage = true} = {}) {
  const nodes = new Map(), requests = [];
  const entries = new Map();
  const localStorage = {get length() { return entries.size; }, key:index => [...entries.keys()][index] ?? null,
    getItem:key => entries.get(key) ?? null, setItem:(key, value) => entries.set(key, value), removeItem:key => entries.delete(key)};
  function element(id = '') {
    const attributes = new Map(), classes = new Set();
    return {id, children:[], dataset:{}, style:{setProperty() {}}, hidden:false,
      classList:{add:name => classes.add(name), remove:name => classes.delete(name),
        toggle(name, force) { if (force ?? !classes.has(name)) classes.add(name); else classes.delete(name); }},
      addEventListener() {}, setAttribute:(name, value) => attributes.set(name, value),
      getAttribute:name => attributes.get(name) ?? null, removeAttribute:name => attributes.delete(name),
      append(...items) { this.children.push(...items); }, replaceChildren(...items) { this.children = items; },
      querySelector() { return this.children[0] ||= element(); },
      get childElementCount() { return this.children.length; },
    };
  }
  const node = id => { if (!nodes.has(id)) nodes.set(id, element(id)); return nodes.get(id); };
  const context = vm.createContext({document:{getElementById:node, createElement:element,
    body:element(), addEventListener() {}}, window:{addEventListener() {}, crypto:webcrypto, TextEncoder, localStorage},
    location:{search:''}, URLSearchParams, matchMedia:() => ({matches:true}),
    localStorage, setInterval() {}, clearInterval() {},
    fetch:async path => { requests.push(path); return {ok:true, status:200, json:async () => ({items:[]})}; },
  });
  const run = code => vm.runInContext(code, context);
  if (includeStorage) run(fs.readFileSync('static/storage-status.js', 'utf8'));
  run(fs.readFileSync('static/projector.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n'));
  run("projectorReady = true; projectorCsrf = 'fixture-csrf'; projectorActivity = {isActive:() => true, touch() {}};");
  const state = extra => ({sequence:1, phase:'waiting', eligible:10, event_name:'Test event',
    server_time:'2026-11-18T00:00:00Z', prize:'', winner:null,
    prizes:[{id:1, name:'Prize one', quantity:2, has_image:true,
      winners:[{employee_id:'0000001', name:'First'}, {employee_id:'0000002', name:'Second'}]}], ...extra});
  return {node, requests, context, run, state, entries,
    render:value => run(`render(${JSON.stringify(value)})`),
    async settle() { for (let index = 0; index < 20; index++) await Promise.resolve(); },
  };
}

test('unchanged projector snapshots do not rebuild cards, even when server time advances', () => {
  const f = fixture();
  f.run('var renderCount = 0; var originalRenderView = renderView; renderView = () => { renderCount++; originalRenderView(); };');
  f.render(f.state());
  const card = f.node('prizeBoardRows').children[0];
  f.render(f.state({server_time:'2026-11-18T00:00:03Z'}));
  assert.equal(f.run('renderCount'), 1);
  assert.equal(f.node('prizeBoardRows').children[0], card);
});

function drawReplies(f, replies) {
  const posts = [];
  f.context.fetch = async (path, options = {}) => {
    if (options.method !== 'POST') return {ok:true, status:200, headers:{get:() => null}, json:async () => f.state()};
    posts.push({path, options});
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    return {ok:reply.status < 400, status:reply.status,
      headers:{get:name => ({'content-type':reply.type || 'application/json',
        ...reply.headers,
        'x-eventdesk-operation-resolved':reply.receipt || (reply.resolved ? options.headers['X-EventDesk-Operation-Id'] : null)})[name.toLowerCase()] ?? null},
      json:async () => reply.payload || {winner:{winner_id:1, employee_id:'0000001'}},
    };
  };
  return posts;
}

test('projector draw sends a secure UUID and resolves the confirmed operation', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.match(posts[0].options.headers['X-EventDesk-Operation-Id'], /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/);
  assert.equal(posts[0].options.headers['X-CSRF-Token'], 'fixture-csrf');
  assert.equal(f.entries.size, 0);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
});

test('a lost projector draw reply blocks another prize and retries the same UUID', async () => {
  const f = fixture(), posts = drawReplies(f, [new Error('reply lost'), {status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  await f.run('drawProjectorPrize({id:2})');
  assert.equal(posts.length, 1);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].options.headers['X-EventDesk-Operation-Id'], posts[1].options.headers['X-EventDesk-Operation-Id']);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.entries.size, 0);
});

test('a mismatched projector draw receipt remains unresolved and keeps the saved identity', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, receipt:'11111111-1111-4111-8111-111111111111'}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.entries.size, 1);
  assert.match(f.node('projectorMessage').textContent, /confirm|retry/i);
});

test('an unresolved draw exposes Retry draw and locks other prizes even after the winner appears', async () => {
  const f = fixture(); drawReplies(f, [new Error('reply lost')]);
  f.render(f.state()); f.run("selectedPrizeId = 1; currentView = 'draw'; renderView();");
  await f.run('drawProjectorPrize({id:1})');
  f.render(f.state({sequence:2, eligible:0, phase:'winner_announced'}));
  assert.equal(f.node('confirmProjectorDraw').textContent, 'Retry draw');
  assert.equal(f.node('confirmProjectorDraw').disabled, false);
  assert.equal(f.node('prizeNext').disabled, true);
  assert.equal(f.node('showRewards').disabled, true);
  f.run('openPrize(2)');
  assert.equal(f.run('selectedPrizeId'), 1);
});

test('only a matching receipt resolves a business rejection after an uncertain draw', async () => {
  const f = fixture(), posts = drawReplies(f, [new Error('reply lost'),
    {status:409, payload:{error:'Wait'}}, {status:409, payload:{error:'Wait'}, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.entries.size, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(f.entries.size, 0);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(new Set(posts.map(item => item.options.headers['X-EventDesk-Operation-Id'])).size, 1);
});

test('a failed display refresh after a confirmed draw cannot make the draw uncertain', async () => {
  const f = fixture(), posts = drawReplies(f, [{status:200, resolved:true}]);
  const send = f.context.fetch;
  f.context.fetch = (path, options) => options?.method === 'POST' ? send(path, options) : Promise.reject(new Error('display offline'));
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 1);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.entries.size, 0);
  assert.match(f.node('projectorMessage').textContent, /saved/i);
});

test('a synced SQL state re-enables the original draw retry without resolving its saved UUID', async () => {
  const f = fixture();
  const display = f.state({prizes:[{id:1, name:'Prize one', quantity:1, has_image:false, winners:[]}]});
  f.render(display); f.run("selectedPrizeId = 1; currentView = 'draw'; renderView();");
  drawReplies(f, [{status:503, payload:{error:'Draws are unavailable offline.'},
    headers:{'x-eventdesk-storage':'sqlite', 'x-eventdesk-sync':'pending'}}]);
  const send = f.context.fetch;
  let synced = false;
  f.context.fetch = (path, options) => options?.method === 'POST' ? send(path, options) : Promise.resolve({
    ok:true, status:200, headers:{get:name => ({'x-eventdesk-storage':'sql',
      ...(synced ? {'x-eventdesk-sync':'synced'} : {})})[name.toLowerCase()] ?? null}, json:async () => display});
  await f.run('drawProjectorPrize({id:1})');
  const savedId = [...f.entries.values()][0];
  assert.equal(f.node('confirmProjectorDraw').disabled, true);
  await f.run('loadState()');
  assert.equal(f.node('confirmProjectorDraw').disabled, true, 'a cached SQL read without sync proof cannot clear offline status');
  synced = true; await f.run('loadState()');
  assert.equal(f.context.window.EventDeskStorage.current().offline, false);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.node('confirmProjectorDraw').disabled, false);
  assert.equal(f.node('confirmProjectorDraw').textContent, 'Retry draw');
  assert.deepEqual([...f.entries.values()], [savedId], 'GET never completes the pending mutation');
});

test('an invalid success reply and unconfirmed server failure retain the draw UUID', async () => {
  for (const reply of [{status:200, type:'text/html'}, {status:503, payload:{error:'Offline'}}, {status:200, payload:{}}]) {
    const f = fixture(); drawReplies(f, [reply]);
    await f.run('drawProjectorPrize({id:1})');
    assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
    assert.equal(f.entries.size, 1);
  }
});

test('the projector cannot post a draw without the storage helper', async () => {
  const f = fixture({includeStorage:false}), posts = drawReplies(f, [{status:200, resolved:true}]);
  await f.run('drawProjectorPrize({id:1})');
  assert.equal(posts.length, 0);
  assert.match(f.node('projectorMessage').textContent, /refresh/i);
});

test('projector loads deferred storage helper before the draw script', () => {
  const html = fs.readFileSync('static/projector.html', 'utf8');
  assert.match(html, /<script defer src="\/static\/storage-status\.js"><\/script>/);
  assert.ok(html.indexOf('/static/storage-status.js') < html.indexOf('/static/projector.js'));
});

test('winner label rotation preserves the reward card and image elements', () => {
  const f = fixture(); f.render(f.state());
  const card = f.node('prizeBoardRows').children[0], visual = card.children[1], image = visual.children[0];
  assert.match(card.children[3].textContent, /First/);
  f.run('boardWinnerIndex += 1; renderPrizeBoard();');
  assert.equal(f.node('prizeBoardRows').children[0], card);
  assert.equal(card.children[1].children[0], image);
  assert.match(card.children[3].textContent, /Second/);
});

test('same-sequence eligibility and prize changes refresh the display', async () => {
  const f = fixture(); f.render(f.state());
  f.render(f.state({eligible:11, prizes:[{id:1, name:'Updated prize', quantity:3, has_image:false, winners:[]}]}));
  await f.settle();
  assert.equal(f.node('eligibleCount').textContent, '11 eligible employees');
  assert.equal(f.node('prizeBoardRows').children[0].title, 'Updated prize');
  assert.equal(f.node('prizeBoardRows').children[0].children.at(-1).style.display, 'none');
  assert.equal(f.requests.filter(path => path === '/api/draw/eligible-ids').length, 1);
});

test('eligibility changes during a pending preview fetch coalesce into one follow-up', async () => {
  const f = fixture(), pending = [];
  f.context.fetch = path => { f.requests.push(path); return new Promise(resolve => pending.push(resolve)); };
  f.render(f.state());
  f.render(f.state({eligible:11})); f.render(f.state({eligible:12})); f.render(f.state({eligible:13}));
  assert.equal(pending.length, 1);
  pending[0]({ok:true, json:async () => ({items:['0000001']})}); await f.settle();
  assert.equal(pending.length, 2);
  pending[1]({ok:true, json:async () => ({items:['0000001','0000002']})}); await f.settle();
  assert.equal(pending.length, 2);
  assert.deepEqual(Array.from(f.run('wheelIds')), ['0000001','0000002']);
});

test('an older draw phase cannot replace the saved winner display', () => {
  const f = fixture();
  f.render(f.state({sequence:2, phase:'winner_announced'}));
  f.render(f.state({sequence:2, phase:'spinning'}));
  f.render(f.state({sequence:1, phase:'winner_announced'}));
  assert.equal(f.run('latestState.sequence'), 2);
  assert.equal(f.run('latestState.phase'), 'winner_announced');
});
