const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

test('check-in camera waits for staff before accepting the next employee', async () => {
  let onScan;
  let scanOptions;
  let scannerFormats;
  class Scanner {
    constructor(id, options) { scannerFormats = options.formatsToSupport; }
    static async getCameras() { return [{id: 'rear', label: 'rear'}]; }
    async start(id, options, callback) { this.isScanning = true; scanOptions = options; onScan = callback; }
    getRunningTrackSettings() { return {width: 640, height: 480}; }
    async applyVideoConstraints() {}
  }
  const context = vm.createContext({
    window: {Html5Qrcode: Scanner, isSecureContext: true}, Html5Qrcode: Scanner,
    Html5QrcodeSupportedFormats: {QR_CODE: 0},
    location: {hostname: 'localhost'}, Date, console,
  });
  const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
  vm.runInContext(fs.readFileSync('static/camera-controls.js', 'utf8'), context);
  vm.runInContext(source, context);
  const seen = [];
  context.recordScan = code => seen.push(code);
  vm.runInContext(`findEmployee = async (workspace, lookup) => {
    recordScan(lookup.code);
    workspace.nextScanTimer = 1;
    return {person: {}};
  }`, context);
  const workspace = {
    mode: 'checkin', scanner: null, reader: {id: 'reader', closest: () => null}, cameraButton: {},
    codeInput: {}, message: {}, scanBusy: false, lastScanCode: '', lastScanAt: 0,
    scanSequence: 0, nextScanTimer: null,
  };
  context.workspace = workspace;
  await vm.runInContext('startCamera(workspace)', context);
  assert.deepEqual(Array.from(scannerFormats), [0]);
  assert.equal(scanOptions.fps, 8);
  assert.equal(scanOptions.qrbox, undefined);
  assert.equal(scanOptions.videoConstraints.deviceId.exact, 'rear');
  assert.equal(scanOptions.videoConstraints.width.ideal, 1920);
  assert.equal(scanOptions.videoConstraints.height.ideal, 1080);
  await onScan('employee-one');
  await onScan('employee-one');
  await onScan('employee-two');
  assert.deepEqual(seen, ['employee-one']);
  workspace.scanBusy = false; // Staff submitted or cleared the first employee.
  await onScan('employee-two');
  assert.deepEqual(seen, ['employee-one', 'employee-two']);
  assert.equal(workspace.scanner.isScanning, true);
});

test('leaving a screen during camera startup does not start the camera afterward', async () => {
  let finishCameras;
  let started = false;
  class Scanner {
    static getCameras() { return new Promise(resolve => { finishCameras = resolve; }); }
    async start() { started = true; }
  }
  const context = vm.createContext({
    window: {Html5Qrcode: Scanner, isSecureContext: true}, Html5Qrcode: Scanner,
    Html5QrcodeSupportedFormats: {QR_CODE: 0},
    location: {hostname: 'localhost'}, console,
  });
  const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
  vm.runInContext(source, context);
  context.workspace = {reader: {id: 'reader', closest: () => null}, cameraButton: {}, message: {}};
  const starting = vm.runInContext('startCamera(workspace)', context);
  await vm.runInContext('stopCamera(workspace)', context);
  finishCameras([{id: 'rear', label: 'rear'}]);
  await starting;
  assert.equal(started, false);
});

function startupFixture(start) {
  const starts = [];
  class Scanner {
    static async getCameras() { return [{id: 'rear', label: 'rear'}]; }
    async start(id, options) {
      starts.push({id, options});
      await start(starts.length, this);
      this.isScanning = true;
    }
    async stop() { this.isScanning = false; }
  }
  const workspace = {mode: 'checkin', reader: {id: 'reader', closest: () => null},
    cameraButton: {}, message: {}, scanBusy: false, scanSequence: 0};
  const context = vm.createContext({workspace, window: {Html5Qrcode: Scanner, isSecureContext: true},
    Html5Qrcode: Scanner, Html5QrcodeSupportedFormats: {QR_CODE: 0},
    location: {hostname: 'localhost'}, Date, console});
  vm.runInContext(fs.readFileSync('static/camera-controls.js', 'utf8'), context);
  vm.runInContext(fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n'), context);
  return {workspace, starts, run: code => vm.runInContext(code, context)};
}

test('a camera rejecting HD constraints retries with its basic supported stream', async () => {
  const f = startupFixture(async attempt => {
    if (attempt === 1) throw Object.assign(new Error('HD unavailable'), {name: 'OverconstrainedError'});
  });
  await f.run('startCamera(workspace)');
  assert.equal(f.starts.length, 2);
  assert.equal(f.starts[0].id, 'rear');
  assert.equal(f.starts[0].options.videoConstraints.width.ideal, 1920);
  assert.equal(f.starts[1].id, 'rear');
  assert.equal(f.starts[1].options.videoConstraints, undefined);
  assert.equal(f.workspace.scanner.isScanning, true);
  assert.equal(f.workspace.cameraButton.textContent, 'Stop camera');
});

for (const name of ['OverconstrainedError', 'ConstraintNotSatisfiedError']) {
  test(`a vendor-wrapped ${name} string retries the basic camera stream`, async () => {
    const f = startupFixture(async attempt => {
      if (attempt === 1) throw `Error getting userMedia, error = ${name}: unsupported HD constraints`;
    });
    await f.run('startCamera(workspace)');
    assert.equal(f.starts.length, 2);
    assert.equal(f.starts[0].options.videoConstraints.width.ideal, 1920);
    assert.equal(f.starts[1].options.videoConstraints, undefined);
    assert.equal(f.workspace.scanner.isScanning, true);
  });
}

test('a vendor-wrapped permission denial does not retry or reopen the camera', async () => {
  const f = startupFixture(async () => {
    throw 'Error getting userMedia, error = NotAllowedError: Permission denied';
  });
  await f.run('startCamera(workspace)');
  assert.equal(f.starts.length, 1);
  assert.notEqual(f.workspace.scanner.isScanning, true);
  assert.equal(f.workspace.cameraButton.disabled, false);
});

for (const rejection of [
  Object.assign(new Error('HD unavailable'), {name: 'OverconstrainedError'}),
  'Error getting userMedia, error = OverconstrainedError: unsupported HD constraints',
]) {
  test(`leaving during a rejected HD startup (${typeof rejection}) does not open a fallback camera afterward`, async () => {
    let rejectStart;
    const f = startupFixture(() => new Promise((_resolve, reject) => { rejectStart = reject; }));
    const pending = f.run('startCamera(workspace)');
    for (let turn = 0; turn < 8 && !rejectStart; turn += 1) await Promise.resolve();
    assert.equal(typeof rejectStart, 'function');
    await f.run('stopCamera(workspace)');
    rejectStart(rejection);
    await pending;
    assert.equal(f.starts.length, 1);
    assert.notEqual(f.workspace.scanner.isScanning, true);
  });
}

test('lookup does not save attendance and identity lookup is read-only', async () => {
  const context = vm.createContext({window: {clearTimeout() {}}, console,
    matchMedia:() => ({matches:false})});
  const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
  vm.runInContext(source, context);
  const requests = [];
  const outcomes = [];
  context.recordRequest = path => requests.push(path);
  context.recordOutcome = (type, title, detail) => outcomes.push({type, title, detail});
  vm.runInContext(`
    api = async path => {
      recordRequest(path);
      return {state: 'done', person: {employee_id: '9900001', name: 'Test Employee', department: 'Test', checked_in_at: path === '/api/scan/checkin' ? '2026-01-01T00:00:00Z' : null}, lookup: {employee_id: '9900001'}};
    };
    renderPerson = (workspace, person) => { workspace.person = person; workspace.empty.hidden = true; workspace.result.hidden = false; };
    setMessage = () => {};
    setOutcome = (workspace, type, title, detail) => recordOutcome(type, title, detail);
    scheduleNextScan = () => {};
    refreshStats = async () => {};
    toast = () => {};
  `, context);
  const workspace = {
    mode: 'checkin', scanSequence: 0, nextScanTimer: null, lookupPending: false,
    actionPending: false, empty: {}, result: {},
    codeInput: {value:'', focus() {}}, idInput: {value:''},
    mobileActions: {}, mobileSubmit: {}, mobileClear: {}, confirm: {},
    modal: {hidden: true, querySelector: selector => modalFields[selector]},
    controlPlaceholder: {}, controlLive: {}, modalMessage: {},
    modalSubmit: {focus() {}}, modalCancel: {focus() {}},
    modalReasonSelect: {value: '', focus() {}},
    modalReasonLabel: {}, modalDetail: {}, modalDetailLabel: {},
  };
  const modalFields = {'.modal-status': {}, '.modal-name': {}, '.modal-id': {}, '.modal-department': {}, '.employee-detail-meta': {}, '.modal-note': {}, '.modal-message': {}};
  context.workspace = workspace;
  await vm.runInContext("findEmployee(workspace, {employee_id:'9900001'})", context);
  assert.deepEqual(requests, ['/api/lookup']);
  assert.equal(workspace.modal.hidden, false);
  assert.equal(workspace.scanBusy, true);
  assert.equal(modalFields['.modal-name'].textContent, 'Test Employee');
  assert.equal(modalFields['.modal-id'].textContent, 'ID 9900001');
  assert.equal(modalFields['.modal-department'].textContent, 'Test');
  assert.equal(workspace.modalReasonLabel.hidden, false);
  assert.equal(workspace.modalReasonSelect.value, 'unreadable');
  workspace.modalReasonSelect.value = '';
  await vm.runInContext('confirmAction(workspace)', context);
  assert.deepEqual(requests, ['/api/lookup']);
  workspace.modalReasonSelect.value = 'unreadable';
  await vm.runInContext('confirmAction(workspace)', context);
  assert.equal(workspace.modal.hidden, true);
  assert.equal(workspace.person, null);
  assert.equal(workspace.scanBusy, false);
  assert.deepEqual(requests, ['/api/lookup', '/api/scan/checkin']);
  workspace.mode = 'verify';
  await vm.runInContext("findEmployee(workspace, {employee_id:'9900001'})", context);
  assert.deepEqual(requests, ['/api/lookup', '/api/scan/checkin', '/api/lookup']);
  assert.equal(outcomes.at(-1).title, 'NOT CHECKED IN');
  assert.equal(outcomes.at(-1).type, 'warning');
  assert.match(outcomes.at(-1).detail, /Test Employee.*9900001.*Test/);
  assert.equal(workspace.result.hidden, false);
  await vm.runInContext('confirmAction(workspace)', context);
  assert.deepEqual(requests, ['/api/lookup', '/api/scan/checkin', '/api/lookup']);
  vm.runInContext(`api = async path => {
    recordRequest(path);
    return {state: path === '/api/checkin/wristband-lost' ? 'updated' : undefined,
      person: {employee_id: '9900001', name: 'Test Employee', department: 'Test', checked_in_at: '2026-01-01T00:00:00Z',
        checkin_wristband_reason: path === '/api/checkin/wristband-lost' ? 'lost' : null}};
  }`, context);
  await vm.runInContext("findEmployee(workspace, {employee_id:'9900001'})", context);
  assert.equal(outcomes.at(-1).title, 'CHECKED IN');
  assert.equal(outcomes.at(-1).type, 'pass');
  assert.match(outcomes.at(-1).detail, /Test Employee.*9900001.*Test/);
  assert.equal(workspace.result.hidden, false);
  assert.deepEqual(requests, ['/api/lookup', '/api/scan/checkin', '/api/lookup', '/api/lookup']);
  workspace.mode = 'checkin';
  await vm.runInContext("findEmployee(workspace, {code:'PARTY:known'})", context);
  assert.equal(workspace.modal.hidden, false, 'repeated QR shows employee information');
  assert.equal(workspace.modalSubmit.hidden, true);
  assert.equal(workspace.scanBusy, false);
  await vm.runInContext("findEmployee(workspace, {employee_id:'9900001'})", context);
  assert.equal(workspace.modal.hidden, false, 'manual duplicate lookup is information-only');
  assert.equal(workspace.modalReasonLabel.hidden, true);
  assert.equal(workspace.scanBusy, false);
  await vm.runInContext('confirmAction(workspace)', context);
  assert.equal(requests.filter(path => path === '/api/scan/checkin').length, 1);
  assert.equal(requests.filter(path => path === '/api/checkin/wristband-lost').length, 0);
});

test('missing QR and employee ID use the same centered result', async () => {
  const dialogs = [];
  const context = vm.createContext({
    window: {clearTimeout() {}},
    console,
  });
  const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
  vm.runInContext(source, context);
  context.recordDialog = (kind, note, title) => dialogs.push({kind, note, title});
  vm.runInContext(`
    api = async () => { const error = new Error('Employee not found.'); error.status = 404; error.code = 'employee_not_found'; throw error; };
    resetWorkspace = workspace => { workspace.lookupRequestId += 1; workspace.lookupPending = false; };
    setOutcome = () => {};
    setMessage = () => {};
    showCheckinDialog = (workspace, kind, person, note, title) => recordDialog(kind, note, title);
  `, context);
  context.workspace = {
    mode: 'checkin', lookupPending: false, actionPending: false, lookupRequestId: 0,
    nextScanTimer: null, scanSequence: 0,
    empty: {}, result: {}, mobileActions: {},
  };
  await vm.runInContext("findEmployee(workspace, {code:'PARTY:unknown'})", context);
  assert.deepEqual(dialogs.at(-1), {kind: 'notfound', note: 'Scan again.', title: 'Employee not found'});
  assert.equal(context.workspace.scanBusy, true);
  await vm.runInContext("findEmployee(workspace, {employee_id:'unknown'})", context);
  assert.deepEqual(dialogs.at(-1), {kind: 'notfound', note: 'Check the employee ID.', title: 'Employee not found'});
});
