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
function fixture(replies) {
  const nodes = new Map();
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {hidden:true, disabled:false, textContent:'', dataset:{},
      classList:{toggle() {}}, setAttribute() {}});
    return nodes.get(id);
  };
  const calls = [];
  const document = {getElementById:node, querySelectorAll:() => [], body:{dataset:{}}};
  const context = vm.createContext({window:{crypto:webcrypto, clearTimeout() {}}, document, Uint8Array, console,
    setTimeout:callback => callback(), fetch:async (path, options) => {
      calls.push({path, options});
      const next = replies.shift();
      if (next instanceof Error) throw next;
      return {ok:next.status < 400, status:next.status,
        headers:headers({'content-type':'application/json', ...next.headers}),
        json:async () => next.payload || {}};
    }});
  vm.runInContext(helperSource(), context);
  vm.runInContext(appSource, context);
  return {calls, node, context, run:code => vm.runInContext(code, context)};
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
  assert.doesNotMatch(helperSource(), /Math\.random|localStorage|sessionStorage/);
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

test('an uncertain mutation retains its identity on manual retry and blocks draws until its result is known', async () => {
  const f = fixture([new Error('lost reply'),
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}},
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced'}, payload:{state:'done'}},
  ]);
  await assert.rejects(f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})"), /Cannot connect/);
  const originalId = f.calls[0].options.headers['X-EventDesk-Operation-Id'];
  assert.equal(f.node('spinButton').disabled, true);
  assert.match(f.node('storageStatus').textContent, /save.*not confirmed/i);
  await f.run("api('/api/stats')");
  assert.equal(f.node('spinButton').disabled, true, 'a healthy GET cannot resolve a lost mutation');
  await f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})");
  assert.equal(f.calls[2].options.headers['X-EventDesk-Operation-Id'], originalId);
  assert.equal(f.node('spinButton').disabled, false);
});

test('only a current explicit coordinator clear can release an uncertain command', async () => {
  const f = fixture([new Error('lost reply'),
    {status:200, headers:{'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-uncertain':'clear'}},
  ]);
  const oldRequest = f.context.window.EventDeskStorage.beginRequest();
  await assert.rejects(f.run("api('/api/draw', {method:'POST', data:{prize_id:1}})"));
  f.context.window.EventDeskStorage.update(headers({'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced',
    'x-eventdesk-uncertain':'clear'}), {}, oldRequest);
  assert.equal(f.node('spinButton').disabled, true);
  await f.run("api('/api/stats')");
  assert.equal(f.node('spinButton').disabled, false);
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
