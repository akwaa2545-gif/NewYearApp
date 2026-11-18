const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {webcrypto} = require('node:crypto');

function fixture(replies = []) {
  let now = 100000;
  const calls = [], entries = new Map();
  const headers = values => ({get:name => values[name.toLowerCase()] ?? null});
  const localStorage = {get length() {return entries.size;}, key:index => [...entries.keys()][index] ?? null,
    getItem:key => entries.get(key) ?? null, setItem:(key, value) => entries.set(key, value), removeItem:key => entries.delete(key)};
  class ClockDate extends Date {static now() {return now;}}
  const window = {crypto:webcrypto, TextEncoder, localStorage, fetch:async (path, options) => {
    calls.push({path, options});
    const reply = replies.shift();
    if (reply instanceof Error) throw reply;
    if (typeof reply === 'function') return reply();
    return {ok:(reply?.status ?? 200) < 400, headers:headers(reply?.headers || {}), body:{async cancel() {}}};
  }};
  const context = vm.createContext({window, Uint8Array, Date:ClockDate});
  vm.runInContext(fs.readFileSync('static/storage-status.js', 'utf8'), context);
  const storage = window.EventDeskStorage;
  const update = (values, payload = {}) => storage.update(headers(values), payload);
  const pending = () => update({'x-eventdesk-storage':'sqlite','x-eventdesk-sync':'pending',
    'x-eventdesk-queue':'pending'}, {pending_sync:true});
  const online = () => update({'x-eventdesk-storage':'sql','x-eventdesk-sync':'online','x-eventdesk-queue':'unknown'});
  return {calls, storage, entries, window, update, pending, online, tick:ms => {now += ms;}};
}

const clear = {'x-eventdesk-storage':'sql','x-eventdesk-sync':'synced','x-eventdesk-queue':'clear'};

test('backup confirmation runs only for active clients with SQL connected and a known pending queue', async () => {
  const f = fixture([{headers:clear}]);
  await f.storage.confirmBackup(true);
  f.pending(); await f.storage.confirmBackup(true);
  f.online(); await f.storage.confirmBackup(false);
  assert.equal(f.calls.length, 0);
  assert.equal(await f.storage.confirmBackup(true), true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].path, '/api/me?backup=1');
  assert.equal(f.calls[0].options.credentials, 'same-origin');
  assert.equal(f.storage.current().queue, 'clear');
  f.tick(60000); await f.storage.confirmBackup(true);
  assert.equal(f.calls.length, 1, 'a cleared queue does not need repeated confirmation');
});

test('failed optional confirmation is limited to once a minute and never disables SQL actions', async () => {
  const f = fixture([new Error('Durable Object quota'), {headers:clear}]);
  f.pending(); f.online();
  assert.equal(await f.storage.confirmBackup(true), false);
  for (let index = 0; index < 100; index++) await f.storage.confirmBackup(true);
  assert.equal(f.calls.length, 1);
  assert.equal(f.storage.current().offline, false);
  assert.equal(f.storage.current().queue, 'pending');
  f.tick(59999); await f.storage.confirmBackup(true);
  assert.equal(f.calls.length, 1);
  f.tick(1); assert.equal(await f.storage.confirmBackup(true), true);
  assert.equal(f.calls.length, 2);
});

test('a confirmation request in flight cannot overlap another after a minute passes', async () => {
  let release;
  const f = fixture([() => new Promise(resolve => {release = resolve;})]);
  f.pending(); f.online();
  const first = f.storage.confirmBackup(true);
  f.tick(61000); await f.storage.confirmBackup(true);
  assert.equal(f.calls.length, 1);
  release({ok:true, headers:{get:name => clear[name.toLowerCase()] ?? null}});
  assert.equal(await first, true);
});

test('only an HTTP success with SQL, synced and clear proves the backup queue drained', async () => {
  const f = fixture([
    {status:503, headers:clear},
    {headers:{...clear, 'x-eventdesk-storage':'sqlite'}},
    {headers:{...clear, 'x-eventdesk-sync':'online'}},
    {headers:{...clear, 'x-eventdesk-queue':'unknown'}},
    {headers:clear},
  ]);
  f.pending(); f.online();
  for (let index = 0; index < 4; index++) {
    assert.equal(await f.storage.confirmBackup(true), false);
    assert.equal(f.storage.current().queue, 'pending');
    assert.equal(f.storage.current().offline, false);
    f.tick(60000);
  }
  assert.equal(await f.storage.confirmBackup(true), true);
});

test('queue confirmation never completes an uncertain draw UUID', async () => {
  const f = fixture([{headers:clear}]);
  const id = await f.storage.prepareOperation('draw-one');
  f.storage.markUncertain(id);
  f.pending(); f.online();
  await f.storage.confirmBackup(true);
  assert.equal(f.storage.current().queue, 'clear');
  assert.equal(f.storage.current().uncertain, true);
  assert.equal(f.entries.size, 1);
  assert.equal(await f.storage.prepareOperation('draw-one'), id);
  await assert.rejects(f.storage.prepareOperation('draw-two'), /retry the same action/i);
});

test('an older confirmation cannot clear a newer accepted offline check-in', async () => {
  let release;
  const f = fixture([() => new Promise(resolve => {release = resolve;})]);
  f.pending(); f.online();
  const confirm = f.storage.confirmBackup(true);
  f.pending();
  release({ok:true, headers:{get:name => clear[name.toLowerCase()] ?? null}});
  assert.equal(await confirm, false);
  assert.equal(f.storage.current().queue, 'pending');
  assert.equal(f.storage.current().offline, true);
});

test('the optional probe has a one-shot deadline and never changes SQL availability on timeout', async () => {
  let rejectFetch, deadline, delay, cleared = 0;
  const f = fixture([() => new Promise((_resolve, reject) => {rejectFetch = reject;})]);
  f.window.AbortController = class {
    constructor() {this.signal = {};}
    abort() {rejectFetch(new Error('aborted'));}
  };
  f.window.setTimeout = (callback, milliseconds) => {deadline = callback; delay = milliseconds; return 7;};
  f.window.clearTimeout = id => {assert.equal(id, 7); cleared += 1;};
  f.pending(); f.online();
  const confirmation = f.storage.confirmBackup(true);
  assert.equal(delay, 8000);
  assert.equal(f.calls[0].options.signal !== undefined, true);
  deadline();
  assert.equal(await confirmation, false);
  assert.equal(cleared, 1);
  assert.equal(f.storage.current().offline, false);
  assert.equal(f.storage.current().queue, 'pending');
});
