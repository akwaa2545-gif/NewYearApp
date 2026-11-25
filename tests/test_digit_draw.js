const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(reducedMotion = false) {
  const frames = new Map(), events = [];
  let clock = 0, ticket = 0;
  function element() {
    const names = new Set();
    return {children:[], style:{setProperty() {}}, setAttribute() {},
      classList:{add:name => names.add(name), remove:(...list) => list.forEach(name => names.delete(name)),
        toggle:(name, active) => active ? names.add(name) : names.delete(name), contains:name => names.has(name)},
      append(item) { this.children.push(item); }, replaceChildren(...items) { this.children = items; }};
  }
  const ctx = vm.createContext({window:{}, document:{createElement:element}, performance:{now:() => clock},
    requestAnimationFrame:cb => { frames.set(++ticket, cb); return ticket; }, cancelAnimationFrame:id => frames.delete(id)});
  vm.runInContext(fs.readFileSync('static/digit-draw.js', 'utf8'), ctx);
  const host = element(), draw = ctx.window.EventDeskDigitDraw.create(host, {reducedMotion,onTick:e => events.push(e)});
  const strips = host.children.map(cell => cell.children[0].children[0]);
  const centerRow = strip => -parseFloat(strip.style.transform.slice(11)) / 100;
  return {draw, strips, frames, events,
    positions:() => strips.map(strip => centerRow(strip) - 1),
    triplet:i => {
      const strip = strips[i], center = centerRow(strip);
      return [-1, 0, 1].map(offset => strip.children[center + offset].textContent).join('');
    },
    active:() => host.children.filter(cell => cell.classList.contains('is-active')).length,
    at(time) { clock = time; const queued = [...frames.values()]; frames.clear(); queued.forEach(cb => cb(clock)); }};
}

const stops = [5000, 5400, 5800, 6200, 6600, 7000, 8000];

test('all strips roll together and settle left to right between their neighbours', () => {
  const f = fixture(), id = '0926809';
  let completed = 0;
  f.draw.play(id, () => completed++);
  assert.equal(f.active(), 7);
  for (let i = 0; i < 7; i++) assert.equal(f.triplet(i), '901');
  f.at(120);
  assert.ok(f.positions().every(row => row > 0));
  for (let i = 0; i < 7; i++) {
    f.at(stops[i] - 100);
    const before = f.positions();
    assert.equal(f.active(), 7 - i);
    f.at(stops[i]);
    const after = f.positions();
    assert.deepEqual(after.slice(0, i), before.slice(0, i));
    assert.ok(after.slice(i + 1).every((row, n) => row > before[i + 1 + n]));
    const target = Number(id[i]);
    assert.equal(after[i], 20 + target);
    assert.equal(f.triplet(i), [(target + 9) % 10, target, (target + 1) % 10].join(''));
    assert.equal(f.active(), 6 - i);
    assert.equal(completed, i === 6 ? 1 : 0);
  }
  assert.deepEqual(f.events.filter(e => e.phase === 'stop').map(e => e.index), [0,1,2,3,4,5,6]);
  assert.equal(f.frames.size, 0);
});

test('each strip eases in and slows into its target', () => {
  stops.forEach((stop, i) => {
    const f = fixture();
    f.draw.play('0926809');
    const travel = time => {
      f.at(time); const from = f.positions()[i];
      f.at(time + 100); return f.positions()[i] - from;
    };
    const launch = travel(0), cruise = travel(1000);
    const brake = travel(stop - 1000), settle = travel(stop - 100);
    assert.ok(launch > 0 && launch < cruise && cruise > brake && brake > settle && settle > 0);
  });
});

test('reset cancels callbacks, resets every strip, and allows the same draw to replay', () => {
  const f = fixture();
  let finished = 0;
  f.draw.play('0000042', () => finished++);
  f.at(4000);
  assert.equal(f.draw.play('0000042'), true);
  assert.equal(f.draw.play('0000001'), false);
  assert.equal(f.frames.size, 1);
  f.draw.reset();
  f.at(60000);
  assert.equal(finished, 0);
  assert.deepEqual(f.positions(), Array(7).fill(0));
  f.draw.play('0000042', () => finished++);
  f.at(120000);
  assert.equal(finished, 1);
  assert.equal(f.positions().map(r => r % 10).join(''), '0000042');
});

test('reduced motion and saved winners display the exact ID immediately', () => {
  const f = fixture(true);
  let called = false;
  f.draw.play('42', () => { called = true; });
  assert.ok(called);
  assert.equal(f.frames.size, 0);
  assert.equal(f.positions().join(''), '0000042');
  assert.equal(f.triplet(0), '901');
  assert.equal(f.draw.show('9999999'), true);
  assert.equal(f.triplet(6), '890');
  assert.equal(f.draw.show('invalid'), false);
});
