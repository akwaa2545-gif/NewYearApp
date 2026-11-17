const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

const appSource = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const helperSource = () => fs.readFileSync('static/storage-status.js', 'utf8');
const headers = values => ({get:name => values[name.toLowerCase()] ?? null});
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function helper(crypto = webcrypto) {
  const context = vm.createContext({window:{crypto}, Uint8Array});
  vm.runInContext(helperSource(), context);
  return context.window.EventDeskStorage;
}
function memoryStorage() {
  const entries = new Map();
  return {get length() {return entries.size;}, key:index => [...entries.keys()][index] ?? null,
    getItem:key => entries.get(key) ?? null, setItem:(key, value) => entries.set(key, value),
    removeItem:key => entries.delete(key), entries};
}
function fixture(replies, localStorage = memoryStorage()) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden:true, disabled:false, textContent:'', dataset:{},
      classList:{toggle() {}}, setAttribute() {}});
    return nodes.get(id);
  };
  const calls = [];
  const document = {getElementById:node, querySelectorAll:() => [], body:{dataset:{}}};
  const context = vm.createContext({window:{crypto:webcrypto, TextEncoder, localStorage, clearTimeout() {}}, document, Uint8Array, console,
    setTimeout:callback => callback(), fetch:async (path, options) => {
      calls.push({path, options});
      const next = replies.shift();
      if (next instanceof Error) throw next;
      return {ok:next.status < 400, status:next.status,
        headers:headers({'content-type':'application/json', ...next.headers,
          ...(next.resolved ? {'x-eventdesk-operation-resolved':options.headers['X-EventDesk-Operation-Id']} : {})}),
        json:async () => next.payload || {}};
    }});
  vm.runInContext(helperSource(), context);
  vm.runInContext(appSource, context);
  return {calls, node, context, localStorage, run:code => vm.runInContext(code, context)};
}

test('request IDs use crypto randomUUID or secure random bytes and never Math.random', () => {
  const modern = helper();
  const first = modern.operationId(), second = modern.operationId();
  assert.match(first, uuid); assert.match(second, uuid); assert.notEqual(first, second);
  let reads = 0;
  const fallback = helper({getRandomValues(bytes) {reads += 1; bytes.fill(255); return bytes;}});
  assert.equal(fallback.operationId(), 'ffffffff-ffff-4fff-bfff-ffffffffffff');
  assert.equal(reads, 1);
  assert.throws(() => helper({}).operationId(), /secure request ID/i);
  assert.doesNotMatch(helperSource(), /Math\.random|sessionStorage/);
});

test('POST creates one operation ID before lookup retries and a separate ID for the next command', async () => {
  const f = fixture([{status:503}, {status:200}, {status:200}]);
  await f.run("api('/api/lookup', {method:'POST', data:{employee_id:'1234567'}})");
  const first = f.calls[0].options.headers['X-EventDesk-Operation-Id'];
  assert.match(first, uuid);
  assert.equal(f.calls[1].options.headers['X-EventDesk-Operation-Id'], first);
  await f.run("api('/api/scan/checkin', {method:'POST', data:{employee_id:'1234567'}})");
  assert.match(f.calls[2].options.headers['X-EventDesk-Operation-Id'], uuid);
  assert.notEqual(f.calls[2].options.headers['X-EventDesk-Operation-Id'], first);
});

test('transport retry reuses operation ID; GET requests do not receive one', async () => {
  const f = fixture([new Error('network'), {status:200}, {status:200}]);
  await f.run("api('/api/lookup', {method:'POST', data:{code:'1234567'}})");
  assert.equal(f.calls[0].options.headers['X-EventDesk-Operation-Id'],
    f.calls[1].options.headers['X-EventDesk-Operation-Id']);
  await f.run("api('/api/stats')");
  assert.equal(f.calls[2].options.headers['X-EventDesk-Operation-Id'], undefined);
});

test('offline status remains until SQL explicitly reports fully synced', () => {
  const status = helper();
  status.update(headers({'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending'}), {});
  assert.equal(status.current().offline, true);
  status.update(headers({'x-eventdesk-storage':'sql'}), {});
  assert.equal(status.current().offline, true);
  status.update(headers({}), {});
  assert.equal(status.current().offline, true);
  status.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'recovering'}), {});
  assert.equal(status.current().offline, true);
  status.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}), {});
  assert.equal(status.current().offline, false);
});

test('pending response shows accessible banner and blocks draws until sync completes', async () => {
  const f = fixture([
    {status:202, headers:{'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending'}, payload:{pending_sync:true}},
    {status:200},
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}},
  ]);
  await f.run("api('/api/scan/checkin', {method:'POST', data:{employee_id:'1234567'}})");
  assert.equal(f.node('storageStatus').hidden, false);
  assert.match(f.node('storageStatus').textContent, /Offline copy.*Check-ins will sync/);
  assert.equal(f.node('spinButton').disabled, true);
  await f.run("api('/api/stats')");
  assert.equal(f.node('storageStatus').hidden, false, 'missing headers cannot clear pending state');
  await f.run("api('/api/stats')");
  assert.equal(f.node('storageStatus').hidden, true);
  assert.equal(f.node('spinButton').disabled, false);
  const html = fs.readFileSync('static/index.html', 'utf8');
  assert.match(html, /id="storageStatus"[^>]*role="status"[^>]*aria-live="polite"/);
  assert.ok(html.indexOf('/static/storage-status.js') < html.indexOf('/static/app.js'));
});

test('offline check-in receipt and announcement say saved offline, without confirmed animation', () => {
  const f = fixture([]);
  f.run(`globalThis.receiptFields = new Map();
    globalThis.work = {receipt:{hidden:true,querySelector(selector){if(!receiptFields.has(selector)) receiptFields.set(selector,{textContent:''}); return receiptFields.get(selector);}},
      announcement:{textContent:''},successFeedback:{hidden:true},root:{querySelector(){return null;}}};
    resetWorkspace = () => {}; employeePopupOpen = () => false;
    finishCheckin(work, {employee_id:'1234567',name:'Example',department:'IT'}, 'pending', 'Will sync when connected.');`);
  assert.equal(f.run("receiptFields.get('.receipt-status').textContent"), 'Saved offline');
  assert.match(f.run('work.announcement.textContent'), /saved offline/i);
  assert.equal(f.run('work.successFeedback.hidden'), true);
});

test('an older SQL response cannot clear a newer offline check-in', () => {
  const status = helper();
  const oldRequest = status.beginRequest();
  const checkin = status.beginRequest();
  status.update(headers({'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending'}), {pending_sync:true}, checkin);
  status.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}), {}, oldRequest);
  assert.equal(status.current().offline, true);
  status.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}), {}, status.beginRequest());
  assert.equal(status.current().offline, false);
});

test('an uncertain mutation retains its identity and offers only a guarded draw retry', async () => {
  const f = fixture([new Error('lost reply'),
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}},
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}, payload:{state:'done'}},
  ]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})"), /Cannot connect/);
  const originalId = f.calls[0].options.headers['X-EventDesk-Operation-Id'];
  assert.equal(f.node('spinButton').textContent, 'Retry draw');
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.match(f.node('storageStatus').textContent, /save.*not confirmed/i);
  await f.run("api('/api/stats')");
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true, 'a healthy GET cannot resolve a lost mutation');
  await f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})");
  assert.equal(f.calls[2].options.headers['X-EventDesk-Operation-Id'], originalId);
  assert.equal(f.node('spinButton').disabled, false);
});

test('even a current coordinator clear cannot release an unrelated uncertain command', async () => {
  const f = fixture([new Error('lost reply'),
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-uncertain':'clear'}},
  ]);
  const oldRequest = f.context.window.EventDeskStorage.beginRequest();
  await assert.rejects(f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})"));
  f.context.window.EventDeskStorage.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced',
    'x-eventdesk-uncertain':'clear'}), {}, oldRequest);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  await f.run("api('/api/stats')");
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.node('storageStatus').hidden, false);
});

test('unconfirmed operation survives reload, persists only digest and UUID metadata, and resolves specifically', async () => {
  const saved = memoryStorage();
  const first = fixture([new Error('lost reply')], saved);
  first.run("state.csrf = 'secret-csrf'");
  await assert.rejects(first.run("api('/api/draw', {method:'POST', data:{prize_id:1, code:'secret-qr',employee_id:'1234567'}})"));
  const originalId = first.calls[0].options.headers['X-EventDesk-Operation-Id'];
  const persisted = JSON.stringify([...saved.entries]);
  assert.doesNotMatch(persisted, /secret-csrf|secret-qr|1234567|prize_id|\/api\/draw/);
  assert.match([...saved.entries.keys()][0], /[a-f0-9]{64}$/);
  const second = fixture([{status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}}], saved);
  second.run("state.csrf = 'secret-csrf'");
  assert.equal(second.context.window.EventDeskStorage.current().uncertain, true);
  await second.run("api('/api/draw', {method:'POST', data:{prize_id:1, code:'secret-qr',employee_id:'1234567'}})");
  assert.equal(second.calls[0].options.headers['X-EventDesk-Operation-Id'], originalId);
  assert.equal(saved.length, 0);
  assert.equal(second.node('spinButton').disabled, false);
});

test('identity is saved before dispatch without marking a healthy in-flight request uncertain', async () => {
  const f = fixture([{status:200}]);
  f.context.fetch = async (_path, options) => {
    assert.equal(f.localStorage.length, 1);
    assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
    assert.match(options.headers['X-EventDesk-Operation-Id'], uuid);
    return {ok:true,status:200,headers:headers({'content-type':'application/json'}),json:async () => ({})};
  };
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(f.localStorage.length, 0);
});

test('storage write failure retains the same in-memory retry identity', async () => {
  const saved = memoryStorage();
  saved.setItem = () => {throw new Error('QuotaExceededError');};
  const f = fixture([new Error('lost reply'), {status:200}], saved);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(f.calls[0].options.headers['X-EventDesk-Operation-Id'], f.calls[1].options.headers['X-EventDesk-Operation-Id']);
});

test('unavailable browser storage does not warn when the API succeeds', async () => {
  const f = fixture([{status:200}]);
  Object.defineProperty(f.context.window, 'localStorage', {get() {throw new Error('SecurityError');}});
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.node('storageStatus').hidden, true);
});

test('a browser denying stored-value reads falls back without claiming an API failure', async () => {
  const denied = {length:1,key:() => `eventdesk.pending.v1.${'a'.repeat(64)}`,
    getItem() {throw new Error('SecurityError');},setItem() {throw new Error('SecurityError');}};
  const f = fixture([{status:200}], denied);
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.node('storageStatus').hidden, true);
});

test('durable prepare and resolution use the same cross-tab browser lock', async () => {
  const f = fixture([{status:200}]);
  const locks = [];
  f.context.window.navigator = {locks:{async request(name, callback) {locks.push(name); return callback();}}};
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(locks.length, 2);
  assert.equal(locks[0], locks[1]);
});

test('pending capacity rejects a new identity without discarding existing identities', async () => {
  const f = fixture([]);
  const service = f.context.window.EventDeskStorage;
  const firstId = await service.prepareOperation('request-0');
  for (let index = 1; index < 256; index += 1) await service.prepareOperation(`request-${index}`);
  assert.equal(service.current().uncertain, false, 'preparing healthy requests must not claim a lost reply');
  await assert.rejects(service.prepareOperation('over-capacity'), /too many saves/i);
  assert.equal(f.localStorage.length, 256);
  assert.equal(await service.prepareOperation('request-0'), firstId);
});

test('specific success cannot resolve another uncertain operation', () => {
  const status = helper();
  status.markUncertain('first');
  status.markUncertain('second');
  status.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-uncertain':'clear'}), {}, null, 'first');
  assert.equal(status.current().uncertain, true);
  status.update(headers({}), {}, null, 'second');
  assert.equal(status.current().uncertain, false);
});

test('delayed old receipt never deletes a newer same-action identity from another tab', async () => {
  const saved = memoryStorage();
  const a = fixture([], saved), b = fixture([], saved);
  const firstId = await a.context.window.EventDeskStorage.prepareOperation('same-action');
  const key = [...saved.entries.keys()][0];
  const originalRecord = saved.getItem(key);
  const shared = fixture([], saved);
  assert.equal(await shared.context.window.EventDeskStorage.prepareOperation('same-action'), firstId);
  shared.context.window.EventDeskStorage.update(headers({}), {}, null, firstId);
  const newerId = await b.context.window.EventDeskStorage.prepareOperation('same-action');
  a.context.window.EventDeskStorage.update(headers({}), {}, null, firstId);
  assert.notEqual(newerId, firstId);
  assert.equal(JSON.parse(saved.getItem(key)).id, newerId);
  assert.notEqual(saved.getItem(key), originalRecord);
});

test('cross-tab newer identity cannot orphan the local original identity', async () => {
  const saved = memoryStorage();
  const a = fixture([], saved);
  const service = a.context.window.EventDeskStorage;
  const originalId = await service.prepareOperation('same-action');
  service.markUncertain(originalId);
  const key = [...saved.entries.keys()][0];
  const newerId = webcrypto.randomUUID();
  saved.setItem(key, JSON.stringify({id:newerId,created:Date.now()}));
  assert.equal(await service.prepareOperation('same-action'), originalId);
  service.update(headers({}), {}, null, originalId);
  assert.equal(await service.prepareOperation('same-action'), newerId);
  service.update(headers({}), {}, null, newerId);
  assert.equal(service.current().uncertain, false);
});

test('observed cross-tab identity remains retryable even when its record disappears before an older reply', async () => {
  const saved = memoryStorage();
  const f = fixture([], saved);
  const service = f.context.window.EventDeskStorage;
  const originalId = await service.prepareOperation('same-action');
  const key = [...saved.entries.keys()][0], newerId = webcrypto.randomUUID();
  saved.setItem(key, JSON.stringify({id:newerId,created:Date.now()}));
  assert.equal(await service.prepareOperation('same-action'), originalId);
  saved.removeItem(key);
  await service.completeOperation(originalId);
  service.update(headers({}), {});
  assert.equal(await service.prepareOperation('same-action'), newerId);
  await service.completeOperation(newerId);
  service.update(headers({}), {});
  assert.equal(service.current().uncertain, false);
});

test('request identity and transmitted CSRF use the same snapshot across async preparation', async () => {
  const f = fixture([{status:200}]);
  f.run("state.csrf = 'original-token'");
  f.context.window.navigator = {locks:{async request(_name, callback) {
    f.run("state.csrf = 'new-token'");
    return callback();
  }}};
  await f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})");
  assert.equal(f.calls[0].options.headers['X-CSRF-Token'], 'original-token');
});

test('authoritative SQL error receipt resolves only the matching lost request', async () => {
  const f = fixture([new Error('lost reply'), {status:409, resolved:true,
    headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'},payload:{error:'No participants'}}]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"), /No participants/);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  assert.equal(f.localStorage.length, 0);
});

test('matching authoritative SQL 5xx receipt is a known outcome while mismatched receipt stays uncertain', async () => {
  const f = fixture([new Error('lost reply'), {status:500,resolved:true,payload:{error:'Application rejected the save'}}]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"), /Application rejected/);
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, false);
  const wrong = fixture([{status:200,headers:{'x-eventdesk-operation-resolved':webcrypto.randomUUID()}}]);
  await assert.rejects(wrong.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"), /cannot confirm/i);
  assert.equal(wrong.context.window.EventDeskStorage.current().uncertain, true);
});

test('SQL error without its operation receipt cannot clear an unresolved request', async () => {
  const f = fixture([new Error('lost reply'), {status:409,
    headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'},payload:{error:'Not confirmed'}}]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  assert.equal(f.context.window.EventDeskStorage.current().uncertain, true);
  assert.equal(f.localStorage.length, 1);
});

test('a reward consumed by the unconfirmed draw remains selectable for exact retry after reload', async () => {
  const saved = memoryStorage();
  const first = fixture([new Error('lost reply')], saved);
  await assert.rejects(first.run("api('/api/draw', {method:'POST',data:{prize_id:'1',round_name:''}})"));
  const f = fixture([{status:200,payload:{prizes:[{id:1,name:'Final reward',active:true,remaining:0}]}}], saved);
  const select = f.node('prizeSelect');
  select.value = '1'; select.options = [];
  select.replaceChildren = () => {select.options = [];};
  select.append = option => select.options.push(option);
  f.context.document.createElement = () => ({dataset:{}});
  f.run('loadRewards = async () => {}');
  await f.run('loadPrizeList()');
  assert.equal(select.options[1].disabled, false);
});

test('draw form retry blocks changed inputs before any projector or draw request', async () => {
  const f = fixture([new Error('lost reply')]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:'1',round_name:'Original'}})"));
  f.node('prizeSelect').selectedOptions = [{dataset:{prizeName:'Reward'}}];
  f.node('prizeSelect').value = '2';
  f.node('roundInput').value = 'Changed';
  await f.run('drawWinner({preventDefault(){}})');
  assert.equal(f.calls.length, 1);
  assert.match(f.node('drawMessage').textContent, /retry the same action/i);
});

test('draw form retry uses the original ID without a projector write first', async () => {
  const f = fixture([new Error('lost reply'), {status:200,payload:{winner:{employee_id:'123',name:'Winner'}}}]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST',data:{prize_id:'1',round_name:'Original'}})"));
  f.node('prizeSelect').selectedOptions = [{dataset:{prizeName:'Reward'}}];
  f.node('prizeSelect').value = '1';
  f.node('roundInput').value = 'Original';
  f.node('luckyWheel').style = {};
  f.run('refreshStats = async () => {}; loadPrizeList = async () => {}; loadProjectorState = async () => {}; loadWinnerHistory = async () => {}');
  await f.run('drawWinner({preventDefault(){}})');
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1].path, '/api/draw');
  assert.equal(f.calls[1].options.headers['X-EventDesk-Operation-Id'], f.calls[0].options.headers['X-EventDesk-Operation-Id']);
});

test('old unresolved identities never expire silently and reject another unsafe action', async () => {
  const saved = memoryStorage();
  const first = fixture([new Error('lost reply')], saved);
  await assert.rejects(first.run("api('/api/draw', {method:'POST',data:{prize_id:1}})"));
  for (const [key, value] of saved.entries) saved.setItem(key, JSON.stringify({...JSON.parse(value),created:1}));
  const next = fixture([], saved);
  assert.equal(next.context.window.EventDeskStorage.current().uncertain, true);
  await assert.rejects(next.run("api('/api/admin/employees', {method:'POST',data:{name:'different'}})"), /retry the same action/i);
  assert.equal(next.calls.length, 0);
  assert.equal(saved.length, 1);
});

test('optional offline backup failure keeps SQL online actions available', async () => {
  const f = fixture([{status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-backup':'unavailable'}},
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-backup':'stale'}},
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-backup':'current'}}]);
  await f.run("api('/api/stats')");
  assert.equal(f.node('storageStatus').textContent, 'Connected · Offline backup unavailable');
  assert.equal(f.node('spinButton').disabled, false);
  assert.equal(f.context.window.EventDeskStorage.current().backup, 'unavailable');
  await f.run("api('/api/stats')");
  assert.equal(f.node('storageStatus').textContent, 'Connected · Offline backup is updating');
  await f.run("api('/api/stats')");
  assert.equal(f.node('storageStatus').hidden, true);
});

test('the confirmation flow treats a queued duplicate as pending rather than a confirmed check-in', async () => {
  const f = fixture([{status:202, headers:{'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending'},
    payload:{pending_sync:true, state:'already_done', person:{employee_id:'1234567',name:'Example',department:'IT'}}}]);
  f.run(`globalThis.receiptFields = new Map();
    globalThis.work = {mode:'checkin',person:{employee_id:'1234567'},lookup:{code:'example-qr'},
      scanSequence:1,lookupRequestId:1,confirm:{},mobileSubmit:{},mobileClear:{},
      receipt:{hidden:true,querySelector(selector){if(!receiptFields.has(selector)) receiptFields.set(selector,{textContent:''}); return receiptFields.get(selector);}},
      announcement:{textContent:''},successFeedback:{hidden:true}};
    resetWorkspace = () => {}; employeePopupOpen = () => false; setCheckinSaving = () => {};`);
  await f.run('confirmAction(work)');
  assert.equal(f.run("receiptFields.get('.receipt-status').textContent"), 'Saved offline');
  assert.match(f.run('work.announcement.textContent'), /will sync when connected/i);
  assert.equal(f.run('work.successFeedback.hidden'), true);
  assert.equal(f.calls.length, 1);
});

test('sync completion preserves an active draw or image-upload lock', () => {
  const f = fixture([]);
  const service = f.context.window.EventDeskStorage;
  service.update(headers({'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending'}), {});
  f.run('state.drawing = true');
  service.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}), {});
  assert.equal(f.node('spinButton').disabled, true);
  f.run('state.drawing = false; state.prizeImageUploading = true; updateDrawAvailability()');
  assert.equal(f.node('spinButton').disabled, true);
  f.run('state.prizeImageUploading = false; updateDrawAvailability()');
  assert.equal(f.node('spinButton').disabled, false);
});
