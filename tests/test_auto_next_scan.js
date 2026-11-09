const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const checkedAt = '2026-01-01T00:00:00Z';
const person = (id, checked = false, reason = null) => ({
  employee_id: id, name: `Employee ${id}`, department: 'IT',
  checked_in_at: checked ? checkedAt : null, checkin_wristband_reason: reason,
});

function scannerFixture(respond) {
  let clock = 10000;
  let nextTimer = 0;
  let onScan;
  const timers = new Map();
  const requests = [];
  const node = () => ({hidden:false, value:'', focus() {}, setAttribute() {}, classList:{toggle() {}}});
  const fields = new Map();
  const field = selector => {
    if (!fields.has(selector)) fields.set(selector, node());
    return fields.get(selector);
  };
  class Scanner {
    static async getCameras() { return [{id: 'rear', label: 'rear'}]; }
    async start(_id, _options, callback) { this.isScanning = true; onScan = callback; }
  }
  class ClockDate extends Date {
    static now() { return clock; }
  }
  const window = {
    Html5Qrcode: Scanner, isSecureContext: true,
    clearTimeout(id) { timers.delete(id); },
    setTimeout(callback, delay) {
      const id = ++nextTimer;
      timers.set(id, {callback, at: clock + delay});
      return id;
    },
  };
  const workspace = {
    mode: 'checkin', scanner: null, reader: {id: 'reader', closest: () => null},
    root: {querySelector: field}, modal: {hidden: true, querySelector: field},
    popup:{open:false, showModal() {this.open = true;}, close() {this.open = false;}},
    receipt:{hidden:true, querySelector:field},
    outcome: {hidden: true, querySelector: field}, message: node(),
    cameraButton: node(), empty: node(), result: node(),
    codeInput: node(), idInput: node(), confirm: node(),
    mobileActions: node(), mobileSubmit: node(), mobileClear: node(),
    modalSubmit: node(), modalCancel: node(), modalReasonSelect: node(),
    modalReasonLabel: node(), modalDetail: node(), modalDetailLabel: node(),
    modalMessage: node(), controlPlaceholder: node(), controlLive: node(),
    person: null, lookup: null, scanBusy: false, lookupPending: false,
    actionPending: false, scanSequence: 0, nextScanTimer: null,
    lastScanCode: '', lastScanAt: 0,
  };
  const context = vm.createContext({
    window, workspace, Date: ClockDate, console, Html5Qrcode: Scanner,
    Html5QrcodeSupportedFormats: {QR_CODE: 0}, location: {hostname: 'localhost'},
    matchMedia: () => ({matches: false}),
    request: async (path, options) => {
      requests.push({path, data: options?.data});
      return respond(path, options?.data);
    },
  });
  vm.runInContext(source, context);
  vm.runInContext('api = request; refreshStats = async () => {};', context);
  const run = code => vm.runInContext(code, context);
  return {
    workspace, requests, timers, run,
    start: () => run('startCamera(workspace)'),
    scan: code => onScan(code),
    advance(ms) {
      clock += ms;
      for (const [id, timer] of [...timers]) {
        if (timer.at <= clock) { timers.delete(id); timer.callback(); }
      }
    },
  };
}

test('an already-checked-in camera scan shows information and immediately accepts the next employee', async () => {
  const f = scannerFixture((_path, data) => ({person: person(data.code, data.code === 'first')}));
  await f.start();
  await f.scan('first');
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.equal(f.workspace.modal.hidden, false);
  assert.equal(f.workspace.popup.open, true);
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.match(f.workspace.modal.querySelector('.modal-name').textContent, /first/);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.scanner.isScanning, true);
  await f.scan('first');
  assert.equal(f.requests.length, 1, 'the same QR remains suppressed for five seconds');
  f.advance(10000);
  await f.scan('first');
  assert.equal(f.requests.length, 1, 'a displayed checked QR stays suppressed beyond the camera cooldown');
  assert.equal(f.workspace.scanBusy, false);
  await f.scan('second');
  assert.equal(f.workspace.person.employee_id, 'second');
  assert.deepEqual(f.requests.map(item => item.path), ['/api/lookup', '/api/lookup']);
});

test('a new employee still waits for staff confirmation before accepting another camera scan', async () => {
  const f = scannerFixture((_path, data) => ({person: person(data.code)}));
  await f.start();
  await f.scan('first');
  assert.equal(f.timers.size, 0);
  f.advance(10000);
  await f.scan('second');
  assert.equal(f.workspace.person.employee_id, 'first');
  assert.equal(f.requests.length, 1);
  assert.equal(f.workspace.modal.hidden, false);
});

for (const result of ['done', 'already_done']) {
  test(`a successful ${result} confirmation immediately makes the scanner ready`, async () => {
    const f = scannerFixture((path, data) => path === '/api/lookup'
      ? {person: person(data.code)} : {state: result, person: person(data.code, true)});
    await f.start();
    await f.scan('first');
    await f.run('confirmAction(workspace)');
    const duplicate = result === 'already_done';
    assert.equal(f.workspace.person?.employee_id || null, duplicate ? 'first' : null);
    assert.equal(f.workspace.modal.hidden, !duplicate);
    assert.equal(f.workspace.popup.open, duplicate);
    assert.equal(f.workspace.receipt.hidden, duplicate);
    if (duplicate) assert.equal(f.workspace.modalSubmit.hidden, true);
    else assert.match(f.workspace.receipt.querySelector('.receipt-name').textContent, /first/);
    assert.equal(f.workspace.scanBusy, false);
    await f.scan('second');
    assert.equal(f.workspace.person.employee_id, 'second');
    assert.deepEqual(f.requests.map(item => item.path), ['/api/lookup', '/api/scan/checkin', '/api/lookup']);
  });
}

test('an already-checked-in manual ID shows information only and remains ready', async () => {
  const f = scannerFixture(() => ({person: person('manual', true)}));
  await f.run("findEmployee(workspace, {employee_id:'manual'})");
  assert.equal(f.workspace.modal.hidden, false);
  assert.equal(f.workspace.popup.open, true);
  assert.equal(f.workspace.scanBusy, false);
  assert.equal(f.workspace.modalSubmit.hidden, true);
  assert.equal(f.workspace.modalReasonLabel.hidden, true);
  assert.match(f.workspace.modal.querySelector('.modal-status').textContent, /already checked in/i);
  assert.match(f.workspace.modal.querySelector('.modal-name').textContent, /manual/);
  await f.run('confirmAction(workspace)');
  assert.deepEqual(f.requests.map(r => r.path), ['/api/lookup']);
});

test('a direct check-in confirmation cannot mark an already-checked-in wristband lost', async () => {
  const f = scannerFixture(() => { throw new Error('No write is allowed'); });
  f.workspace.person = person('checked', true);
  f.workspace.lookup = {employee_id:'checked'};
  await f.run('confirmAction(workspace)');
  assert.equal(f.requests.length, 0);
  assert.equal(f.workspace.actionPending, false);
});
