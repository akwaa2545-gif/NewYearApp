const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('static/app.js', 'utf8').replace(/\ninitialize\(\);\s*$/, '\n');
const photo = {type: 'image/jpeg', size: 1024};

function photoScanner(decode = async () => 'PARTY:photo-code') {
  const calls = {pick: 0, cameras: 0, constructors: 0, decode: [], lookups: [], messages: []};
  class Scanner {
    constructor() { calls.constructors += 1; this.isScanning = false; }
    static async getCameras() { calls.cameras += 1; return []; }
    async scanFile(...args) { calls.decode.push(args); return decode(...args); }
    async clear() {}
  }
  const workspace = {
    mode: 'checkin', scanner: null, reader: {id: 'reader', closest: () => null},
    cameraButton: {disabled: false}, photoInput: {value: '', click() { calls.pick += 1; }},
    codeInput: {value: ''}, message: {}, scanBusy: false, lookupPending: false,
    actionPending: false, scanSequence: 0, cameraGeneration: 0,
    lastScanCode: '', lastScanAt: 0, nextScanTimer: null,
  };
  const context = vm.createContext({
    workspace, window: {Html5Qrcode: Scanner, isSecureContext: false, clearTimeout() {}},
    Html5Qrcode: Scanner, Html5QrcodeSupportedFormats: {QR_CODE: 0},
    location: {hostname: '192.0.2.10'}, console,
    recordLookup: lookup => calls.lookups.push({...lookup}),
    recordMessage: (message, type) => calls.messages.push({message, type}),
  });
  vm.runInContext(source, context);
  vm.runInContext(`
    findEmployee = async (_workspace, lookup) => { recordLookup(lookup); return {person: {}}; };
    setMessage = (_workspace, message, type) => recordMessage(message, type);
  `, context);
  return {context, workspace, calls, Scanner};
}

function scan(context, file) {
  context.file = arguments.length < 2 ? photo : file;
  return vm.runInContext('scanQrPhoto(workspace, file)', context);
}

test('HTTP camera button opens the native photo picker synchronously without enumerating cameras', async () => {
  const {context, calls} = photoScanner();
  const starting = vm.runInContext('startCamera(workspace)', context);
  assert.equal(calls.pick, 1);
  await starting;
  assert.equal(calls.cameras, 0);
});

test('a QR photo populates the scan and looks up the employee for staff review', async () => {
  const {context, workspace, calls} = photoScanner();
  await scan(context);
  assert.equal(calls.constructors, 1);
  assert.equal(calls.decode.length, 1);
  assert.equal(calls.decode[0][0], photo);
  assert.equal(calls.decode[0][1], false);
  assert.equal(workspace.codeInput.value, 'PARTY:photo-code');
  assert.deepEqual(calls.lookups, [{code: 'PARTY:photo-code'}]);
  assert.equal(workspace.scanBusy, true);
  assert.equal(workspace.cameraButton.disabled, false);
});

test('photo decoding reuses the workspace scanner instance', async () => {
  const {context, workspace, calls, Scanner} = photoScanner();
  const existing = workspace.scanner = new Scanner();
  await scan(context);
  assert.equal(workspace.scanner, existing);
  assert.equal(calls.constructors, 1);
  assert.equal(calls.decode.length, 1);
});

test('cancelling the photo picker does not decode or look up an employee', async () => {
  const {context, calls} = photoScanner();
  await scan(context, undefined);
  await scan(context, null);
  assert.equal(calls.decode.length, 0);
  assert.equal(calls.lookups.length, 0);
  assert.equal(calls.messages.length, 0);
});

for (const [description, file] of [
  ['a non-image file', {type: 'application/zip', size: 1024}],
  ['a photo larger than 20 MiB', {type: 'image/jpeg', size: 20 * 1024 * 1024 + 1}],
]) {
  test(`photo scanning rejects ${description} before decoding`, async () => {
    const {context, workspace, calls} = photoScanner();
    await scan(context, file);
    assert.equal(calls.decode.length, 0);
    assert.equal(calls.lookups.length, 0);
    assert.ok(calls.messages.some(({message, type}) => type === 'error' && /photo|image|20/i.test(message)));
    assert.equal(workspace.scanBusy, false);
    assert.equal(workspace.cameraButton.disabled, false);
  });
}

test('a photo without a readable QR code shows a helpful error and permits another scan', async () => {
  const {context, workspace, calls} = photoScanner(async () => { throw new Error('decoder failure'); });
  await scan(context);
  assert.equal(calls.lookups.length, 0);
  assert.ok(calls.messages.some(({message, type}) => type === 'error' && /QR|photo|image/i.test(message)));
  assert.equal(workspace.scanBusy, false);
  assert.equal(workspace.cameraButton.disabled, false);
});

test('leaving the screen while a photo decodes discards its result', async () => {
  let finish;
  const {context, workspace, calls} = photoScanner(() => new Promise(resolve => { finish = resolve; }));
  const pending = scan(context);
  await Promise.resolve();
  assert.equal(calls.decode.length, 1);
  await vm.runInContext('stopCamera(workspace)', context);
  finish('PARTY:old-photo');
  await pending;
  assert.equal(calls.lookups.length, 0);
  assert.equal(workspace.codeInput.value, '');
  assert.equal(workspace.scanBusy, false);
  assert.equal(workspace.cameraButton.disabled, false);
});

test('selecting another photo during decoding does not start a second scan', async () => {
  let finish;
  const {context, calls} = photoScanner(() => new Promise(resolve => { finish = resolve; }));
  const pending = scan(context);
  await Promise.resolve();
  await scan(context);
  assert.equal(calls.decode.length, 1);
  finish('PARTY:first-photo');
  await pending;
  assert.deepEqual(calls.lookups, [{code: 'PARTY:first-photo'}]);
});

for (const busy of ['scanBusy', 'lookupPending', 'actionPending']) {
  test(`photo scanning waits while ${busy} is active`, async () => {
    const {context, workspace, calls} = photoScanner();
    workspace[busy] = true;
    await scan(context);
    assert.equal(calls.decode.length, 0);
    assert.equal(calls.lookups.length, 0);
  });
}
