const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const jsQR = require('../static/vendor/jsQR.js');
const fixture = require('./fixtures/qr_matrix.json');

const {qrFrame} = require('./fixtures/qr_frame.js');

function worker(decoder = jsQR) {
  const messages = [], imports = [], calls = [];
  const self = {postMessage:value => messages.push(value)};
  const context = vm.createContext({self, Uint8ClampedArray, Uint32Array, console,
    importScripts:path => {
      imports.push(path);
      if (path !== '/static/vendor/jsQR.js') throw new Error('Unexpected decoder dependency');
      context.jsQR = (...args) => {calls.push(args); return decoder(...args);};
    },
  });
  vm.runInContext(fs.readFileSync('static/qr-detail-worker.js','utf8'),context);
  return {messages,imports,calls,send:data => self.onmessage({data})};
}

test('worker decodes a real synthetic QR with the vendored decoder', async () => {
  const w = worker();
  await w.send({id: 1, ...qrFrame(), enhance: false});
  assert.deepEqual(w.imports, ['/static/vendor/jsQR.js']);
  assert.equal(w.messages[0].id, 1);
  assert.equal(w.messages[0].code, fixture.code);
  assert.equal(w.calls.length, 1);
  assert.equal(w.calls[0][3].inversionAttempts, 'dontInvert');
  assert.equal(w.messages[0].data, undefined, 'pixels must not be returned to the app');
});

test('source-resolution small QR remains readable when a 320px thumbnail loses its modules', async () => {
  const source = qrFrame({width: 1280, height: 720, scale: 3});
  const thumbnail = new Uint8ClampedArray(320 * 180 * 4);
  for (let y = 0; y < 180; y += 1) {
    for (let x = 0; x < 320; x += 1) {
      const target = (y * 320 + x) * 4;
      const original = (y * 4 * source.width + x * 4) * 4;
      thumbnail.set(source.data.subarray(original, original + 4), target);
    }
  }
  assert.equal(jsQR(thumbnail, 320, 180, {inversionAttempts: 'dontInvert'}), null);
  const w = worker();
  await w.send({id: 2, ...source, enhance: false});
  assert.equal(w.messages[0].code, fixture.code);
});

test('bounded contrast enhancement recovers a dark low-contrast QR', async () => {
  const frame = qrFrame({dark: 30, light: 48});
  assert.equal(jsQR(frame.data, frame.width, frame.height, {inversionAttempts: 'dontInvert'}), null);
  const w = worker();
  await w.send({id: 3, ...frame, enhance: true});
  assert.equal(w.messages[0].code, fixture.code);
  assert.equal(w.calls.length, 2);
  assert.equal(w.calls[1][3].inversionAttempts, 'attemptBoth');
  assert.equal(frame.data[0], 48, 'enhancement must not mutate the transferred source array');
});

test('uniform or already-high-contrast misses avoid unnecessary extra decoding', async () => {
  const w = worker(() => null);
  const uniform = new Uint8ClampedArray(20 * 20 * 4).fill(24);
  await w.send({id: 4, data: uniform, width: 20, height: 20, enhance: true});
  assert.equal(w.calls.length, 1);
  const frame = qrFrame();
  await w.send({id: 5, ...frame, enhance: true});
  assert.equal(w.calls.length, 2);
  assert.equal(w.messages.every(value => value.code === null), true);
});

test('malformed frames are rejected before any decoder call', async () => {
  const w = worker(() => { throw new Error('must not be called'); });
  for (const data of [
    {width: 0, height: 10, data: new Uint8ClampedArray()},
    {width: 1281, height: 10, data: new Uint8ClampedArray()},
    {width: 1280, height: 1280, data: new Uint8ClampedArray()},
    {width: 2.5, height: 10, data: new Uint8ClampedArray()},
    {width: Infinity, height: 10, data: new Uint8ClampedArray()},
    {width: 2, height: 2, data: new Uint8ClampedArray(15)},
    {width: 2, height: 2, data: new Uint8Array(16)},
  ]) {
    await w.send({id: 6, ...data});
    assert.equal(w.messages.at(-1).code, null);
    assert.equal(w.messages.at(-1).error, 'Unable to decode frame.');
  }
  assert.equal(w.calls.length, 0);
});

test('decoder errors produce a fixed message without private details', async () => {
  const w = worker(() => { throw new Error('private stack or pixel details'); });
  await w.send({id: 7, ...qrFrame()});
  assert.equal(w.messages[0].id, 7);
  assert.equal(w.messages[0].code, null);
  assert.equal(w.messages[0].error, 'Unable to decode frame.');
  assert.doesNotMatch(JSON.stringify(w.messages), /private|pixel|stack/);
});

