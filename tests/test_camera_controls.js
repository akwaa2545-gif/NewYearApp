const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

function fixture(capabilities = {}, settings = {}, apply = async () => {}) {
  const calls = [];
  const element = () => ({hidden: false, disabled: false, value: '', attributes: {},
    setAttribute(name, value) { this.attributes[name] = value; }});
  const workspace = {
    cameraGeneration: 1, torchButton: element(), zoomControl: element(),
    zoomInput: element(), cameraHint: element(),
    scanner: {
      getRunningTrackCapabilities: () => capabilities,
      getRunningTrackSettings: () => ({deviceId: 'rear', width: 1920, height: 1080, frameRate: 24, ...settings}),
      async applyVideoConstraints(value) { calls.push(value); await apply(value); },
    },
  };
  const context = vm.createContext({window: {}, console});
  vm.runInContext(fs.readFileSync('static/camera-controls.js', 'utf8'), context);
  return {camera: context.window.EventDeskCamera, workspace, calls};
}

test('camera selection prefers the normal rear camera over ultra-wide and telephoto lenses', () => {
  const {camera} = fixture();
  const cameras = [{id: 'front', label: 'Front'}, {id: 'ultra', label: 'Back Ultra Wide'},
    {id: 'normal', label: 'Back Camera'}, {id: 'tele', label: 'Back Telephoto'}];
  assert.equal(camera.selectCamera(cameras).id, 'normal');
  assert.equal(camera.selectCamera(cameras.slice(0, 2)).id, 'ultra');
  assert.equal(camera.selectCamera([{id: 'one', label: ''}, {id: 'two', label: ''}]).id, 'two');
  assert.equal(camera.selectCamera([]), undefined);
});

test('initial constraints request HD capture without making resolution mandatory', () => {
  const {camera} = fixture();
  const value = camera.constraints('rear');
  assert.equal(value.deviceId.exact, 'rear');
  assert.equal(value.width.ideal, 1920);
  assert.equal(value.height.ideal, 1080);
  assert.equal(value.width.exact, undefined);
  assert.equal(value.height.exact, undefined);
  assert.equal(value.frameRate.max, 30);
});

test('camera constraints prefer the rear camera when an exact device is unavailable', () => {
  const {camera} = fixture();
  const value = camera.constraints();
  assert.equal(value.facingMode.ideal, 'environment');
  assert.equal(value.deviceId, undefined);
  assert.equal(camera.constraints('rear').facingMode, undefined);
});

test('camera preview stays muted and inline on iPhone', async () => {
  const {camera, workspace} = fixture();
  const attributes = {};
  const video = {setAttribute(name, value) { attributes[name] = value; }};
  workspace.reader = {querySelector: () => video};
  await camera.configure(workspace, 1);
  assert.equal(video.muted, true);
  assert.equal(video.playsInline, true);
  assert.equal(attributes.playsinline, '');
  assert.equal(attributes['webkit-playsinline'], '');
});

test('only supported camera controls are shown and existing zoom is respected', async () => {
  const {camera, workspace, calls} = fixture({torch: true, zoom: {min: 1, max: 5, step: 0.25}}, {zoom: 1.5});
  await camera.configure(workspace, 1);
  assert.equal(workspace.torchButton.hidden, false);
  assert.equal(workspace.torchButton.attributes['aria-pressed'], 'false');
  assert.equal(workspace.zoomControl.hidden, false);
  assert.equal(Number(workspace.zoomInput.value), 1.5);
  assert.equal(Number(workspace.zoomInput.step), 0.25);
  assert.equal(calls.length, 0, 'configuration must not turn on the torch or change zoom');
  camera.reset(workspace);
  assert.equal(workspace.torchButton.hidden, true);
  assert.equal(workspace.zoomControl.hidden, true);
});

test('unsupported and malformed capabilities leave controls hidden without breaking scanning', async () => {
  const {camera, workspace, calls} = fixture({torch: false, zoom: {min: NaN, max: 5}});
  await camera.configure(workspace, 1);
  assert.equal(workspace.torchButton.hidden, true);
  assert.equal(workspace.zoomControl.hidden, true);
  assert.equal(await camera.toggleTorch(workspace), false);
  assert.equal(await camera.setZoom(workspace, 2), false);
  assert.equal(calls.length, 0);
  workspace.scanner.getRunningTrackCapabilities = () => { throw new Error('not implemented'); };
  await camera.configure(workspace, 1);
  assert.equal(workspace.torchButton.hidden, true);
});

test('continuous focus, exposure and white balance fail independently and retain accepted modes', async () => {
  const {camera, workspace, calls} = fixture({focusMode: ['continuous'], exposureMode: ['continuous'],
    whiteBalanceMode: ['continuous']}, {}, async value => {
    if (value.exposureMode === 'continuous') throw new Error('exposure unsupported');
  });
  await camera.configure(workspace, 1);
  assert.equal(calls.length, 3);
  assert.equal(calls[0].focusMode, 'continuous');
  assert.equal(calls[1].exposureMode, 'continuous');
  assert.equal(calls[2].whiteBalanceMode, 'continuous');
  assert.equal(calls[2].exposureMode, undefined);
  assert.equal(calls[2].focusMode, 'continuous');
  assert.match(workspace.cameraHint.textContent, /adjust|focus|light/i);
});

test('torch and zoom changes preserve the selected camera, capture resolution and each other', async () => {
  const {camera, workspace, calls} = fixture({torch: [true, false], zoom: {min: 1, max: 4, step: 0.5}, focusMode: ['continuous']});
  await camera.configure(workspace, 1);
  assert.equal(await camera.toggleTorch(workspace), true);
  assert.equal(workspace.torchButton.attributes['aria-pressed'], 'true');
  assert.equal(await camera.setZoom(workspace, 100), true);
  const last = calls.at(-1);
  assert.equal(last.deviceId.exact, 'rear');
  assert.equal(last.width.ideal, 1920);
  assert.equal(last.height.ideal, 1080);
  assert.equal(last.focusMode, 'continuous');
  assert.equal(last.advanced[0].torch, true);
  assert.equal(last.advanced[0].zoom, 4);
  assert.equal(Number(workspace.zoomInput.value), 4);
});

test('failed changes retain the confirmed torch and zoom values and report a useful hint', async () => {
  const {camera, workspace} = fixture({torch: true, zoom: {min: 1, max: 4}}, {zoom: 2}, async () => { throw new Error('not supported'); });
  await camera.configure(workspace, 1);
  assert.equal(await camera.toggleTorch(workspace), false);
  assert.equal(workspace.torchButton.attributes['aria-pressed'], 'false');
  workspace.zoomInput.value = '3';
  assert.equal(await camera.setZoom(workspace, 3), false);
  assert.equal(Number(workspace.zoomInput.value), 2);
  assert.equal(workspace.zoomInput.disabled, false);
  assert.match(workspace.cameraHint.textContent, /cannot|could not|unable/i);
});

test('a pending control change is serialized and cannot update a stopped or restarted camera', async () => {
  let finish;
  const {camera, workspace, calls} = fixture({torch: true, zoom: {min: 1, max: 4}}, {},
    () => new Promise(resolve => { finish = resolve; }));
  await camera.configure(workspace, 1);
  const pending = camera.toggleTorch(workspace);
  assert.equal(workspace.torchButton.disabled, false);
  assert.equal(workspace.torchButton.attributes['aria-disabled'], 'true');
  assert.equal(await camera.setZoom(workspace, 3), false);
  assert.equal(calls.length, 1);
  workspace.cameraGeneration = 2;
  camera.reset(workspace);
  await camera.configure(workspace, 2);
  finish();
  assert.equal(await pending, false);
  assert.equal(workspace.torchButton.hidden, false);
  assert.equal(workspace.torchButton.attributes['aria-pressed'], 'false');
  assert.equal(workspace.torchButton.disabled, false);
});

test('a stale configuration stops after its pending operation and leaves replacement controls untouched', async () => {
  let finish;
  const {camera, workspace, calls} = fixture({torch: true, focusMode: ['continuous'], exposureMode: ['continuous']}, {},
    () => new Promise(resolve => { finish = resolve; }));
  const pending = camera.configure(workspace, 1);
  workspace.cameraGeneration = 2;
  camera.reset(workspace);
  workspace.cameraHint.textContent = 'New camera';
  finish();
  await pending;
  assert.equal(calls.length, 1);
  assert.equal(workspace.torchButton.hidden, true);
  assert.equal(workspace.cameraHint.textContent, 'New camera');
});

test('busy camera controls retain keyboard focus and reject repeated changes without losing confirmed values', async () => {
  let finish;
  const {camera, workspace, calls} = fixture({torch: true, zoom: {min: 1, max: 4}}, {zoom: 1.5},
    () => new Promise(resolve => { finish = resolve; }));
  await camera.configure(workspace, 1);
  let focused = workspace.zoomInput;
  for (const control of [workspace.torchButton, workspace.zoomInput]) {
    let disabled = false;
    Object.defineProperty(control, 'disabled', {
      get: () => disabled,
      set(value) { disabled = value; if (value && focused === control) focused = null; },
    });
  }
  workspace.zoomInput.value = '3';
  const pending = camera.setZoom(workspace, 3);
  assert.equal(focused, workspace.zoomInput);
  assert.equal(workspace.zoomInput.disabled, false);
  assert.equal(workspace.torchButton.disabled, false);
  assert.equal(workspace.zoomInput.attributes['aria-disabled'], 'true');
  assert.equal(Number(workspace.zoomInput.value), 1.5, 'pending changes keep the last confirmed value');
  assert.equal(await camera.toggleTorch(workspace), false);
  workspace.zoomInput.value = '4';
  assert.equal(await camera.setZoom(workspace, 4), false);
  assert.equal(Number(workspace.zoomInput.value), 1.5);
  assert.equal(calls.length, 1);
  finish();
  assert.equal(await pending, true);
  assert.equal(Number(workspace.zoomInput.value), 3);
  assert.equal(workspace.zoomInput.attributes['aria-disabled'], 'false');
  assert.equal(workspace.torchButton.attributes['aria-disabled'], 'false');
  assert.equal(focused, workspace.zoomInput);
  camera.reset(workspace);
  assert.equal(workspace.zoomInput.attributes['aria-disabled'], 'false');
  assert.equal(workspace.torchButton.attributes['aria-disabled'], 'false');
});
