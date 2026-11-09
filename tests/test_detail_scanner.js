const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const jsQR = require('../static/vendor/jsQR.js');
const qr = require('./fixtures/qr_matrix.json');

function detailScanner({decode = () => 'PARTY:tiny', width = 1920, height = 1080,
  missingWorker = false, missingContext = false, frameData = null} = {}) {
  let clock = 10000;
  let nextTimer = 0;
  const timers = new Map();
  const children = [];
  const canvases = [];
  const calls = {draws: [], frames: [], workers: [], terminated: 0, codes: [], network: 0};
  const video = {videoWidth: width, videoHeight: height, clientWidth: 320, clientHeight: 180,
    readyState: 4, paused: false, ended: false};
  const node = tag => ({
    tagName: tag.toUpperCase(), id: '', style: {}, hidden: false,
    setAttribute(name, value) { this[name] = value; },
    appendChild(child) { children.push(child); return child; },
    append(child) { children.push(child); },
    remove() { const index = children.indexOf(this); if (index >= 0) children.splice(index, 1); },
  });
  const document = {
    hidden: false,
    body: {appendChild(child) { children.push(child); return child; }, append(child) { children.push(child); }},
    createElement(tag) {
      const element = node(tag);
      if (tag === 'canvas') {
        canvases.push(element);
        const context = {canvas: element, drawImage(...args) { calls.draws.push(args); },
          getImageData(_x, _y, width, height) {
            return {data: frameData ? frameData(calls.draws.at(-1), width, height) :
              new Uint8ClampedArray(width * height * 4), width, height};
          }};
        element.getContext = () => missingContext ? null : context;
      }
      return element;
    },
    getElementById(id) { return children.find(child => child.id === id) || null; },
  };
  class Worker {
    constructor(url) { this.url = url; calls.workers.push(this); }
    postMessage(payload, transfer) {
      calls.frames.push({...payload, transfer});
      Promise.resolve().then(() => decode(payload)).then(
        code => this.onmessage?.({data: {id: payload.id, code}}),
        error => this.onerror?.({message: error.message}),
      );
    }
    terminate() { calls.terminated += 1; }
  }
  class ClockDate extends Date { static now() { return clock; } }
  const window = {
    Worker: missingWorker ? undefined : Worker,
    setTimeout(callback, delay) { const id = ++nextTimer; timers.set(id, {callback, at: clock + delay}); return id; },
    clearTimeout(id) { timers.delete(id); },
  };
  const workspace = {
    reader: {id: 'checkinCameraReader', querySelector: selector => selector === 'video' ? video : null},
    scanner: {isScanning: true, scanFile() { throw new Error('Live scanner must not decode files'); }},
    cameraGeneration: 7, scanSequence: 0, scanBusy: false, lookupPending: false, actionPending: false, lastScanAt: 0,
  };
  const context = vm.createContext({window, document, Date: ClockDate, console, Worker: window.Worker,
    setTimeout: window.setTimeout, clearTimeout: window.clearTimeout,
    fetch() { calls.network += 1; throw new Error('Image decoding must stay on device'); },
  });
  vm.runInContext(fs.readFileSync('static/qr-detail-scanner.js', 'utf8'), context, {filename: 'static/qr-detail-scanner.js'});
  const flush = async () => { for (let turn = 0; turn < 16; turn += 1) await Promise.resolve(); };
  const next = async () => {
    const entry = [...timers].sort((a, b) => a[1].at - b[1].at)[0];
    if (entry) { timers.delete(entry[0]); clock = entry[1].at; entry[1].callback(); }
    await flush();
  };
  const advance = async ms => {
    const end = clock + ms;
    while ([...timers.values()].some(timer => timer.at <= end)) await next();
    clock = end;
    await flush();
  };
  return {workspace, document, calls, timers, children, canvases, video, next, advance, flush,
    nextDelay: () => Math.min(...[...timers.values()].map(timer => timer.at - clock)),
    start: onCode => window.EventDeskDetailScanner.start(workspace, onCode || (code => calls.codes.push(code)), 7)};
}

test('small-QR detail pass uses native center pixels instead of the preview resolution', async () => {
  const f = detailScanner();
  const stop = f.start();
  await f.next();
  assert.equal(f.calls.frames.length, 1);
  const [source, sx, sy, sw, sh, dx, dy, dw, dh] = f.calls.draws[0];
  assert.equal(source, f.video);
  assert.equal(sw, 640);
  assert.equal(sh, 640);
  assert.equal(sx, (1920 - sw) / 2);
  assert.equal(sy, (1080 - sh) / 2);
  assert.equal(dx, 0);
  assert.equal(dy, 0);
  assert.equal(dw, 640, 'preserve native pixels rather than shrinking to the 320px preview');
  assert.equal(dh, 640);
  assert.equal(f.calls.frames[0].width, 640);
  assert.equal(f.calls.frames[0].height, 640);
  assert.equal(f.calls.frames[0].data.length, 640 * 640 * 4);
  assert.equal(f.calls.workers[0].url, '/static/qr-detail-worker.js');
  assert.equal(f.calls.frames[0].transfer[0], f.calls.frames[0].data.buffer, 'transfer the frame to a background worker');
  assert.deepEqual(f.calls.codes, ['PARTY:tiny']);
  assert.equal(f.calls.network, 0);
  stop();
});

test('detail pass cycles native 640 and 1024 center crops then a bounded full-frame overview', async () => {
  const f = detailScanner({width: 3840, height: 2160, decode: () => null});
  const stop = f.start();
  await f.next();
  await f.next();
  await f.next();
  assert.equal(f.calls.draws.length, 3);
  assert.deepEqual(f.calls.draws[0].slice(1), [1600, 760, 640, 640, 0, 0, 640, 640]);
  assert.deepEqual(f.calls.draws[1].slice(1), [1408, 568, 1024, 1024, 0, 0, 1024, 1024]);
  assert.deepEqual(f.calls.draws[2].slice(1), [0, 0, 3840, 2160, 0, 0, 1280, 720]);
  assert.deepEqual(f.calls.frames.map(frame => frame.enhance), [false, true, false]);
  assert.equal(f.calls.codes.length, 0);
  assert.equal(f.calls.network, 0, 'failed image attempts do not call the event API');
  stop();
});

test('square video frames stay within the worker pixel budget after output dimensions are rounded', async () => {
  const f = detailScanner({width: 1920, height: 1920, decode: () => null});
  const stop = f.start();
  await f.next();
  await f.next();
  await f.next();
  assert.equal(f.calls.frames.length, 3, 'exercise both center crops and the full-frame pass');
  for (const frame of f.calls.frames) {
    assert.ok(frame.width <= 1280);
    assert.ok(frame.height <= 1280);
    assert.ok(frame.width * frame.height <= 1280 * 1024,
      `${frame.width}x${frame.height} must fit the background decoder pixel budget`);
  }
  stop();
});

test('a slow decode never overlaps a second frame decode', async () => {
  let finish;
  const f = detailScanner({decode: () => new Promise(resolve => { finish = resolve; })});
  const stop = f.start();
  await f.next();
  assert.equal(f.calls.frames.length, 1);
  await f.advance(2000);
  assert.equal(f.calls.frames.length, 1);
  finish('PARTY:first');
  await f.flush();
  await f.next();
  assert.equal(f.calls.frames.length, 2);
  stop();
  finish('PARTY:late');
  await f.flush();
});

for (const busy of ['scanBusy', 'lookupPending', 'actionPending']) {
  test(`detail decoding waits while ${busy} is active`, async () => {
    const f = detailScanner();
    f.workspace[busy] = true;
    const stop = f.start();
    await f.next();
    assert.equal(f.calls.frames.length, 0);
    f.workspace[busy] = false;
    await f.next();
    assert.equal(f.calls.frames.length, 1);
    stop();
  });
}

test('detail decoding pauses in a hidden tab and resumes when visible', async () => {
  const f = detailScanner();
  f.document.hidden = true;
  const stop = f.start();
  await f.next();
  assert.equal(f.calls.frames.length, 0);
  f.document.hidden = false;
  await f.next();
  assert.equal(f.calls.frames.length, 1);
  stop();
});

test('a recent successful scan suppresses extra detail work for at least 1500ms', async () => {
  const f = detailScanner();
  f.workspace.lastScanAt = 10000;
  const stop = f.start(code => { f.calls.codes.push(code); f.workspace.lastScanAt = 11500; });
  await f.advance(1499);
  assert.equal(f.calls.frames.length, 0);
  await f.advance(1000);
  assert.equal(f.calls.frames.length, 1);
  stop();
});

test('center crops never upscale a lower-resolution camera', async () => {
  const f = detailScanner({width:480, height:360, decode:() => null});
  const stop = f.start();
  await f.next(); await f.next();
  for (const draw of f.calls.draws) assert.deepEqual(draw.slice(1), [60, 0, 360, 360, 0, 0, 360, 360]);
  stop();
});

test('odd video dimensions keep the native crop origin on whole source pixels', async () => {
  const f = detailScanner({width:1921, height:1081, decode:() => null});
  const stop = f.start(); await f.next();
  assert.deepEqual(f.calls.draws[0].slice(1), [640,220,640,640,0,0,640,640]);
  stop();
});

test('fast detail decoding uses a 250ms gap, then backs off for a slower worker', async () => {
  let finish;
  const f = detailScanner({decode:() => new Promise(resolve => {finish = resolve;})});
  const stop = f.start();
  assert.equal(f.nextDelay(), 250);
  await f.next(); await f.advance(40); finish(null); await f.flush();
  assert.equal(f.nextDelay(), 250);
  await f.next(); await f.advance(400); finish(null); await f.flush();
  assert.equal(f.nextDelay(), 600);
  await f.next(); await f.advance(1800); finish(null); await f.flush();
  assert.equal(f.nextDelay(), 1000);
  await f.next(); await f.advance(30); finish(null); await f.flush();
  assert.equal(f.nextDelay(), 250, 'cadence recovers when the worker is fast again');
  assert.equal(f.calls.frames.length, 4, 'no queued or overlapping frames');
  stop();
});

test('a two-pixel-module QR in a 4K source decodes from the native crop that the old resized crop loses', async () => {
  // One pixel off-center exposes resize aliasing; native crops retain the same source modules.
  const sourceWidth = 3840, sourceHeight = 2160, moduleSize = 2;
  const left = (sourceWidth - qr.matrix.length * moduleSize) / 2 + 1;
  const top = (sourceHeight - qr.matrix.length * moduleSize) / 2;
  const rasterize = (draw, width, height) => {
    const [, sx, sy, sw, sh] = draw;
    const data = new Uint8ClampedArray(width * height * 4);
    for (let y = 0; y < height; y += 1) {
      for (let x = 0; x < width; x += 1) {
        const column = Math.floor((sx + (x + 0.5) * sw / width - left) / moduleSize);
        const row = Math.floor((sy + (y + 0.5) * sh / height - top) / moduleSize);
        const color = qr.matrix[row]?.[column] ? 0 : 255;
        const index = (y * width + x) * 4;
        data[index] = data[index + 1] = data[index + 2] = color;
        data[index + 3] = 255;
      }
    }
    return data;
  };
  const oldCrop = rasterize([null,1110,270,1620,1620],1024,1024);
  assert.equal(jsQR(oldCrop,1024,1024,{inversionAttempts:'dontInvert'}), null);
  const f = detailScanner({width:sourceWidth, height:sourceHeight, frameData:rasterize,
    decode:frame => jsQR(frame.data,frame.width,frame.height,{inversionAttempts:'dontInvert'})?.data});
  const stop = f.start(); await f.next();
  assert.deepEqual(f.calls.codes,[qr.code]);
  assert.equal(f.calls.frames[0].width * f.calls.frames[0].height,409600);
  stop();
});

test('stopping during decode discards the late result and safely releases its frame buffer', async () => {
  let finish;
  const f = detailScanner({decode: () => new Promise(resolve => { finish = resolve; })});
  const stop = f.start();
  await f.next();
  assert.equal(f.calls.frames.length, 1);
  stop();
  finish('PARTY:stale');
  await f.flush();
  assert.deepEqual(f.calls.codes, []);
  assert.equal(f.children.length, 0);
  assert.equal(f.canvases[0].width, 0);
  assert.equal(f.canvases[0].height, 0);
  assert.equal(f.calls.terminated, 1);
  assert.equal(f.timers.size, 0);
  assert.equal(f.workspace.scanner.isScanning, true, 'detail cleanup does not stop the live camera');
});

test('a replaced camera generation cannot deliver an old detail result', async () => {
  let finish;
  const f = detailScanner({decode: () => new Promise(resolve => { finish = resolve; })});
  const stop = f.start();
  await f.next();
  f.workspace.cameraGeneration += 1;
  finish('PARTY:old-camera');
  await f.flush();
  assert.deepEqual(f.calls.codes, []);
  stop();
  assert.equal(f.children.length, 0);
});

test('resetting or replacing an employee lookup discards an older background QR result', async () => {
  let finish;
  const f = detailScanner({decode: () => new Promise(resolve => { finish = resolve; })});
  const stop = f.start();
  await f.next();
  f.workspace.scanSequence += 1;
  finish('PARTY:previous-employee');
  await f.flush();
  assert.deepEqual(f.calls.codes, [], 'an older frame cannot restart the employee lookup after reset');
  await f.next();
  assert.equal(f.calls.frames.length, 2, 'discarding an older result keeps the scanner available');
  finish('PARTY:current-employee');
  await f.flush();
  assert.deepEqual(f.calls.codes, ['PARTY:current-employee']);
  stop();
});

test('a failed background worker disables extra decoding without stopping the camera', async () => {
  const f = detailScanner({decode: async () => { throw new Error('worker failed'); }});
  const stop = f.start();
  await f.next();
  assert.equal(f.workspace.scanner.isScanning, true);
  assert.equal(f.calls.terminated, 1);
  assert.equal(f.timers.size, 0);
  assert.deepEqual(f.calls.codes, []);
  stop();
  assert.equal(f.calls.terminated, 1, 'cleanup remains idempotent');
});

test('a worker that stops responding is released after its bounded deadline', async () => {
  const f = detailScanner({decode: () => new Promise(() => {})});
  const stop = f.start();
  await f.next();
  await f.advance(5000);
  assert.equal(f.calls.terminated, 1);
  assert.equal(f.workspace.scanner.isScanning, true);
  assert.equal(f.timers.size, 0);
  stop();
});

for (const missing of ['missingWorker', 'missingContext']) {
  test(`unsupported ${missing} leaves the primary scanner running`, async () => {
    const f = detailScanner({[missing]: true});
    const stop = f.start();
    assert.equal(typeof stop, 'function');
    await f.next();
    assert.equal(f.calls.frames.length, 0);
    assert.equal(f.workspace.scanner.isScanning, true);
    stop();
    assert.equal(f.children.length, 0);
  });
}

test('a video without usable dimensions is skipped until its stream is ready', async () => {
  const f = detailScanner({width: 0, height: 0});
  const stop = f.start();
  await f.next();
  assert.equal(f.calls.frames.length, 0);
  f.video.videoWidth = 1920;
  f.video.videoHeight = 1080;
  await f.next();
  assert.equal(f.calls.frames.length, 1);
  stop();
});
