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
      dispatch(name) { for (const callback of listeners.get(name) || []) callback({type:name}); }};
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
    if (page === 'app') run("state.profile = {role:'staff'}; state.view = 'draw'; refreshStats = async () => fetch('/stats'); loadWinnerHistory = async () => fetch('/history'); renderProjectorState = () => {}; startAppBackground(); connectProjectorState();");
    else run('render = () => {}; projectorReady = true; startProjectorBackground(); refreshProjectorBackground();');
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
  test(`${page} closes SSE and makes no automatic requests while hidden, blurred, or idle`, async () => {
    const f = fixture(page); await f.settle();
    assert.equal(f.sources.length, 1);
    for (const reason of ['hidden', 'blur', 'idle']) {
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
      const expected = page === 'app' ? 3 : 2;
      assert.equal(f.calls.length - count, expected, 'one immediate refresh of each current view resource');
      assert.equal(f.sources.filter(source => !source.closed).length, 1);
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
    assert.equal(f.sources.filter(source => !source.closed).length, 1);
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
    f.run(page === 'app' ? 'connectProjectorState()' : 'connect()');
    assert.ok(f.sources.every(source => source.closed));
  });
  test(`${page} replaces terminally closed streams once while active`, async () => {
    const f = fixture(page); await f.settle();
    f.sources[0].close();
    f.run(page === 'app' ? 'updateNetwork()' : 'connect()');
    assert.equal(f.sources.length, 2);
    f.run(page === 'app' ? 'updateNetwork()' : 'connect()');
    assert.equal(f.sources.length, 2);
    f.focus(false); f.run(page === 'app' ? 'updateNetwork()' : 'connect()');
    assert.equal(f.sources.length, 2);
  });
}

test('paused projector board rotation cannot recreate uncached prize image requests', async () => {
  const f = fixture('projector'); await f.settle();
  f.run("boardPrizes = [{}]; renderPrizeBoard = () => fetch('/image');");
  await f.tick(8000); assert.ok(f.calls.includes('/image'));
  f.focus(false); const count = f.calls.length;
  await f.tick(300000); assert.equal(f.calls.length, count);
});

test('a projector draw already submitted finishes and retains its result while paused', async () => {
  const f = fixture('projector'); await f.settle();
  let finishDraw;
  const originalFetch = f.context.fetch;
  f.context.fetch = path => path === '/api/draw' ? new Promise(resolve => { finishDraw = resolve; }) : originalFetch(path);
  f.run('renderView = () => {};');
  const draw = f.run("drawProjectorPrize({id:'reward'})");
  f.focus(false);
  finishDraw({ok:true, json:async () => ({winner:{name:'Saved winner'}})});
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
