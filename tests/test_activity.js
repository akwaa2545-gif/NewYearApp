const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(page, includeActivity = true) {
  let now = 0, nextId = 0, focused = true;
  const timers = new Map(), nodes = new Map(), sources = [], calls = [];
  const target = () => {
    const listeners = new Map();
    return {addEventListener(name, callback) { const group = listeners.get(name) || []; group.push(callback); listeners.set(name, group); },
      dispatch(name, fields = {}) { for (const callback of listeners.get(name) || []) callback({type:name, ...fields}); }};
  };
  const node = id => {
    if (!nodes.has(id)) nodes.set(id, {...target(), hidden:true, dataset:{}, classList:{add() {}, remove() {}, toggle() {}},
      querySelector() { return node(`${id}-span`); }, setAttribute() {}, replaceChildren() {}, style:{}});
    return nodes.get(id);
  };
  const window = target();
  const document = {...target(), hidden:false, hasFocus:() => focused, getElementById:node, body:{dataset:{}}};
  function EventSource() { this.closed = false; this.readyState = 1; this.close = () => { this.closed = true; this.readyState = 2; }; this.addEventListener = () => {}; sources.push(this); }
  window.EventSource = EventSource;
  class ClockDate extends Date { static now() { return now; } }
  const context = vm.createContext({window, document, EventSource, Date:ClockDate, console, navigator:{onLine:true},
    location:{search:''}, URLSearchParams, matchMedia:() => ({matches:true}), localStorage:{getItem() {}},
    setTimeout:(callback, delay) => { const id = ++nextId; timers.set(id, {callback, at:now + delay}); return id; },
    clearTimeout:id => timers.delete(id),
    setInterval:(callback, delay) => { const id = ++nextId; timers.set(id, {callback, at:now + delay, repeat:delay}); return id; },
    clearInterval:id => timers.delete(id), fetch:async path => { calls.push(path); return {ok:true, status:200, json:async () => ({items:[], phase:'waiting'})}; }});
  if (includeActivity) vm.runInContext(fs.readFileSync('static/activity.js', 'utf8'), context);
  const run = code => vm.runInContext(code, context);
  if (page) {
    run(fs.readFileSync(`static/${page}.js`, 'utf8').replace(/\ninitialize\(\);\s*$/, '\n'));
    if (page === 'app') run("state.profile = {role:'staff'}; state.view = 'draw'; refreshStats = async () => fetch('/stats'); loadWinnerHistory = async () => fetch('/history'); loadProjectorState = async () => fetch('/api/projector/state'); renderProjectorState = () => {}; startAppBackground(); connectProjectorState();");
    else run('render = () => {}; projectorReady = true; startProjectorBackground();');
  }
  async function tick(ms) {
    const end = now + ms;
    while (true) {
      const entry = [...timers].filter(([, timer]) => timer.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!entry) break;
      const [id, timer] = entry; now = timer.at;
      if (timer.repeat) timer.at += timer.repeat; else timers.delete(id);
      timer.callback(); await settle();
    }
    now = end; await settle();
  }
  async function settle() { for (let i = 0; i < 12; i++) await Promise.resolve(); }
  return {window, document, sources, calls, run, tick, settle, node, context, jump:ms => { now += ms; },
    focus(value) { focused = value; window.dispatch(value ? 'focus' : 'blur'); }};
}

test('interaction renews the two-minute deadline; focus and visibility cannot duplicate a wake', async () => {
  const f = fixture();
  f.run('var changes = []; var activity = window.EventDeskActivity.create(); activity.subscribe(active => changes.push(active));');
  await f.tick(119000); f.document.dispatch('keydown'); await f.tick(119000);
  assert.equal(f.run('activity.isActive()'), true);
  await f.tick(1000); assert.equal(f.run('activity.isActive()'), false);
  f.document.dispatch('pointerdown'); f.window.dispatch('focus'); f.document.dispatch('visibilitychange');
  assert.deepEqual(Array.from(f.run('changes')), [false, true]);
  f.focus(false); assert.equal(f.run('activity.isActive()'), false);
  f.document.dispatch('input'); assert.equal(f.run('activity.isActive()'), false);
  f.focus(true); assert.equal(f.run('activity.isActive()'), true);
});

for (const page of ['app', 'projector']) {
  test(`${page} offers backup confirmation only from an active background refresh`, async () => {
    const f = fixture(page); await f.settle();
    const confirmations = [];
    f.window.EventDeskStorage = {confirmBackup:active => {confirmations.push(active); return Promise.resolve(false);}};
    if (page === 'app') f.run("state.view = 'checkin';");
    await f.run(page === 'app' ? 'refreshAppBackground()' : 'refreshProjectorState()');
    assert.deepEqual(confirmations, [true]);
    f.focus(false);
    await f.run(page === 'app' ? 'refreshAppBackground()' : 'refreshProjectorState()');
    assert.deepEqual(confirmations, [true], 'a paused page must not start a confirmation probe');
  });
  test(`${page} suspends polling when inactive and resumes once`, async () => {
    const f = fixture(page); await f.settle();
    assert.equal(f.sources.length, 0);
    for (const reason of page === 'projector' ? ['hidden'] : ['hidden', 'blur', 'idle']) {
      if (reason === 'hidden') { f.document.hidden = true; f.document.dispatch('visibilitychange'); }
      if (reason === 'blur') f.focus(false);
      if (reason === 'idle') await f.tick(120000);
      assert.ok(f.sources.every(source => source.closed));
      const count = f.calls.length;
      f.window.dispatch('online');
      for (const source of f.sources) source.onerror?.();
      await f.tick(300000);
      assert.equal(f.calls.length, count);
      if (reason === 'hidden') { f.document.hidden = false; f.document.dispatch('visibilitychange'); }
      else if (reason === 'blur') f.focus(true);
      else f.document.dispatch('keydown');
      await f.settle();
      const expected = page === 'app' ? 3 : 1;
      assert.equal(f.calls.length - count, expected, 'one immediate refresh of each current view resource');
      assert.equal(f.sources.length, 0);
      const resumedCount = f.calls.length, sourceCount = f.sources.length;
      f.window.dispatch('focus'); f.document.dispatch('input'); f.document.dispatch('visibilitychange');
      await f.settle();
      assert.equal(f.calls.length, resumedCount);
      assert.equal(f.sources.length, sourceCount);
    }
  });
  test(`${page} repeated scanner keyboard input prevents idle suspension`, async () => {
    const f = fixture(page); await f.settle();
    for (let i = 0; i < 5; i++) { await f.tick(90000); f.document.dispatch('keydown'); }
    assert.equal(f.sources.length, 0);
    assert.ok(f.calls.length > 0);
  });
  test(`${page} automatic connections fail closed when activity helper is unavailable`, async () => {
    const f = fixture(page, false); await f.settle();
    f.window.dispatch('online'); await f.tick(300000);
    assert.equal(f.calls.length, 0);
    assert.equal(f.sources.length, 0);
  });
  test(`${page} checks idle time before reconnecting even when timers were delayed`, async () => {
    const f = fixture(page); await f.settle();
    f.jump(120001);
    f.run(page === 'app' ? 'connectProjectorState()' : 'refreshProjectorState()');
    assert.ok(f.sources.every(source => source.closed));
  });
  test(`${page} repeated network notifications cannot duplicate polling timers`, async () => {
    const f = fixture(page); await f.settle();
    for (let index = 0; index < 5; index++) f.window.dispatch('online');
    await f.settle();
    f.calls.length = 0;
    await f.tick(15000);
    assert.equal(f.calls.filter(path => path === '/api/projector/state').length, 5);
    assert.equal(f.sources.length, 0);
  });
}

test('one active projector stays within 10,100 background requests over eight hours', async () => {
  const f = fixture('projector'); await f.settle();
  for (let minute = 0; minute < 480; minute++) { f.document.dispatch('keydown'); await f.tick(60000); }
  assert.equal(f.calls.filter(path => path === '/api/projector/state').length, 9601);
  assert.ok(f.calls.length <= 10100);
  assert.equal(f.sources.length, 0);
});

test('an unattended visible projector picks up staff selections, and catches up when shown again', async () => {
  const f = fixture('projector'); await f.settle();
  let prize = 'Tablet';
  f.context.fetch = async () => ({ok:true, status:200, json:async () => ({prize})});
  f.run('render = state => { latestState = state; }');
  f.focus(false);
  await f.tick(3000);
  assert.equal(f.run('latestState.prize'), 'Tablet');
  await f.tick(120001);
  prize = 'Scooter';
  await f.tick(3000);
  assert.equal(f.run('latestState.prize'), 'Scooter');
  f.document.hidden = true; f.document.dispatch('visibilitychange');
  prize = 'Watch';
  await f.tick(6000);
  assert.equal(f.run('latestState.prize'), 'Scooter');
  f.document.hidden = false; f.document.dispatch('visibilitychange');
  await f.settle();
  assert.equal(f.run('latestState.prize'), 'Watch');
});

test('a selection during a poll fetches the latest prize immediately after the old response', async () => {
  const f = fixture('projector'); await f.settle();
  f.run('render = state => { lastRenderedSequence = state.sequence; }');
  const pending = [];
  f.context.fetch = () => new Promise(resolve => pending.push(resolve));
  const respond = sequence => pending.shift()({ok:true, json:async () => ({sequence})});
  const notify = sequence => f.window.dispatch('storage', {key:'eventdesk-projector-selection', newValue:String(sequence)});
  f.focus(false);
  notify(2); notify(3); notify(4);
  assert.equal(pending.length, 1);
  respond(2); await f.settle();
  assert.equal(pending.length, 1);
  respond(4); await f.settle();
  notify(3);
  assert.equal(pending.length, 0);
  assert.equal(f.run('lastRenderedSequence'), 4);
});

test('main draw polls state once per three seconds and summaries once per minute', async () => {
  const f = fixture('app'); await f.settle();
  await f.tick(60000);
  assert.equal(f.calls.filter(path => path === '/api/projector/state').length, 20);
  assert.equal(f.calls.filter(path => path === '/stats').length, 1);
  assert.equal(f.calls.filter(path => path === '/history').length, 1);
  assert.equal(f.sources.length, 0);
});

test('scanner views make no background requests even during continuous activity', async () => {
  const f = fixture('app'); await f.settle();
  f.run("state.view = 'checkin'");
  for (let minute = 0; minute < 60; minute++) { f.document.dispatch('keydown'); await f.tick(60000); }
  assert.equal(f.calls.length, 0);
});

test('paused projector board rotation cannot recreate uncached prize image requests', async () => {
  const f = fixture('projector'); await f.settle();
  f.run("boardPrizes = [{}]; renderPrizeBoard = () => fetch('/image');");
  await f.tick(8000); assert.ok(f.calls.includes('/image'));
  f.document.hidden = true; f.document.dispatch('visibilitychange'); const count = f.calls.length;
  await f.tick(300000); assert.equal(f.calls.length, count);
});

test('a projector draw already submitted finishes and retains its result while paused', async () => {
  const f = fixture('projector'); await f.settle();
  let finishDraw;
  const originalFetch = f.context.fetch;
  f.context.fetch = path => path === '/api/draw' ? new Promise(resolve => { finishDraw = resolve; }) : originalFetch(path);
  f.context.window.EventDeskStorage = {prepareOperation:async () => '11111111-1111-4111-8111-111111111111',
    current:() => ({uncertain:false, offline:false}), beginRequest:() => 1, markUncertain() {},
    completeOperation:async () => true, update() {}};
  f.run('renderView = () => {};');
  const draw = f.run("drawProjectorPrize({id:'reward'})");
  await f.settle();
  f.document.hidden = true; f.document.dispatch('visibilitychange');
  finishDraw({ok:true, status:200, headers:{get:name => name === 'Content-Type' ? 'application/json' : null},
    json:async () => ({winner:{name:'Saved winner'}})});
  await draw;
  assert.equal(f.node('projectorMessage').textContent, 'Draw started');
  assert.equal(f.run('projectorDrawing'), false);
  const count = f.calls.length; await f.tick(300000);
  assert.equal(f.calls.length, count);
});

test('an admin refresh queued before blur cannot dispatch a new batch while paused', async () => {
  const f = fixture('app'); await f.settle();
  const pending = [];
  f.context.fetch = path => new Promise(resolve => pending.push({path, resolve}));
  const first = f.run("state.profile = {role:'admin'}; state.view = 'admin'; refreshAdmin()");
  await f.run('refreshAdmin()');
  assert.equal(pending.length, 3);
  f.focus(false);
  for (const request of pending) request.resolve({ok:true, status:200, headers:{get:() => 'application/json'},
    json:async () => request.path === '/api/admin/activity' ? {items:[]} : {checked_in:0, count:0}});
  await first; await f.settle();
  assert.equal(pending.length, 3);
  assert.equal(f.run('state.adminRefreshPending'), false);
});
