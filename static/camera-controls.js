'use strict';

(() => {
  const states = new WeakMap();
  const rear = camera => /back|rear|environment/i.test(camera.label || '');
  const specialLens = camera => /ultra|wide\s*angle|telephoto|tele\b|macro/i.test(camera.label || '');
  const supported = (values, value) => Array.isArray(values) && values.includes(value);
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

  function selectCamera(cameras) {
    return cameras.find(camera => rear(camera) && !specialLens(camera)) ||
      cameras.find(rear) || cameras[cameras.length - 1];
  }

  function constraints(deviceId) {
    return {
      ...(deviceId ? {deviceId: {exact: deviceId}} : {facingMode: {ideal: 'environment'}}),
      width: {ideal: 1920}, height: {ideal: 1080}, frameRate: {ideal: 24, max: 30},
    };
  }

  function current(workspace, state) {
    return states.get(workspace) === state && workspace.cameraGeneration === state.generation &&
      workspace.scanner === state.scanner;
  }

  function render(workspace, state) {
    if (!current(workspace, state)) return;
    if (workspace.torchButton) {
      workspace.torchButton.hidden = !state.torchSupported;
      workspace.torchButton.disabled = false;
      workspace.torchButton.setAttribute('aria-disabled', String(state.busy));
      workspace.torchButton.textContent = state.torch ? 'Light off' : 'Light on';
      workspace.torchButton.setAttribute('aria-pressed', String(state.torch));
    }
    if (workspace.zoomControl) workspace.zoomControl.hidden = !state.zoomRange;
    if (workspace.zoomInput) {
      workspace.zoomInput.disabled = false;
      workspace.zoomInput.setAttribute('aria-disabled', String(state.busy));
      if (state.zoomRange) {
        workspace.zoomInput.min = String(state.zoomRange.min);
        workspace.zoomInput.max = String(state.zoomRange.max);
        workspace.zoomInput.step = String(state.zoomRange.step);
        workspace.zoomInput.value = String(state.zoom);
        workspace.zoomInput.setAttribute('aria-valuetext', `${state.zoom.toFixed(1)} times`);
      }
    }
  }

  function reset(workspace) {
    states.delete(workspace);
    if (workspace.torchButton) {
      workspace.torchButton.hidden = true;
      workspace.torchButton.disabled = false;
      workspace.torchButton.setAttribute('aria-disabled', 'false');
      workspace.torchButton.textContent = 'Light on';
      workspace.torchButton.setAttribute('aria-pressed', 'false');
    }
    if (workspace.zoomControl) workspace.zoomControl.hidden = true;
    if (workspace.zoomInput) {
      workspace.zoomInput.disabled = false;
      workspace.zoomInput.setAttribute('aria-disabled', 'false');
      workspace.zoomInput.value = '1';
    }
    if (workspace.cameraHint) workspace.cameraHint.textContent = '';
  }

  function optionalValue(scanner, method) {
    try { return scanner[method]?.() || {}; }
    catch (_) { return {}; }
  }

  function videoConstraints(state, changes) {
    const controls = {
      ...state.applied,
      ...(state.torchSupported ? {torch: state.torch} : {}),
      ...(state.zoomRange ? {zoom: state.zoom} : {}),
      ...changes,
    };
    return {...state.base, ...controls, advanced: [{...controls}]};
  }

  async function configure(workspace, generation) {
    if (workspace.cameraGeneration !== generation) return false;
    reset(workspace);
    const video = workspace.reader?.querySelector?.('video');
    if (video) {
      video.muted = true;
      video.playsInline = true;
      video.setAttribute('playsinline', '');
      video.setAttribute('webkit-playsinline', '');
    }
    const scanner = workspace.scanner;
    const capabilities = optionalValue(scanner, 'getRunningTrackCapabilities');
    const settings = optionalValue(scanner, 'getRunningTrackSettings');
    const range = capabilities.zoom;
    const zoomRange = range && Number.isFinite(range.min) && Number.isFinite(range.max) &&
      range.min > 0 && range.max > range.min ? {
        min: range.min, max: range.max,
        step: Number.isFinite(range.step) && range.step > 0 ? range.step : 0.1,
      } : null;
    const state = {
      generation, scanner, applied: {}, busy: true,
      base: {
        ...constraints(settings.deviceId),
        width: {ideal: settings.width || 1920}, height: {ideal: settings.height || 1080},
        frameRate: {ideal: Math.min(settings.frameRate || 24, 30), max: 30},
      },
      torchSupported: capabilities.torch === true || supported(capabilities.torch, true),
      torch: settings.torch === true,
      zoomRange,
      zoom: zoomRange ? clamp(Number.isFinite(settings.zoom) ? settings.zoom : zoomRange.min,
        zoomRange.min, zoomRange.max) : 1,
    };
    states.set(workspace, state);
    render(workspace, state);
    let failed = false;
    for (const name of ['focusMode', 'exposureMode', 'whiteBalanceMode']) {
      if (!supported(capabilities[name], 'continuous')) continue;
      try {
        await scanner.applyVideoConstraints(videoConstraints(state, {[name]: 'continuous'}));
        if (!current(workspace, state)) return false;
        state.applied = {...state.applied, [name]: 'continuous'};
      } catch (_) {
        if (!current(workspace, state)) return false;
        failed = true;
      }
    }
    if (!current(workspace, state)) return false;
    state.busy = false;
    render(workspace, state);
    if (failed && workspace.cameraHint) {
      workspace.cameraHint.textContent = 'Use more light or move the camera.';
    }
    return true;
  }

  async function change(workspace, name, value) {
    const state = states.get(workspace);
    if (!state || !current(workspace, state) || state.busy) {
      if (state) render(workspace, state);
      return false;
    }
    state.busy = true;
    render(workspace, state);
    try {
      await state.scanner.applyVideoConstraints(videoConstraints(state, {[name]: value}));
      if (!current(workspace, state)) return false;
      state[name] = value;
      if (workspace.cameraHint) workspace.cameraHint.textContent = '';
      return true;
    } catch (_) {
      if (current(workspace, state) && workspace.cameraHint) {
        workspace.cameraHint.textContent = `Cannot change ${name === 'torch' ? 'light' : 'zoom'}.`;
      }
      return false;
    } finally {
      if (current(workspace, state)) {
        state.busy = false;
        render(workspace, state);
      }
    }
  }

  function toggleTorch(workspace) {
    const state = states.get(workspace);
    return state?.torchSupported ? change(workspace, 'torch', !state.torch) : Promise.resolve(false);
  }

  function setZoom(workspace, value) {
    const state = states.get(workspace);
    if (!state?.zoomRange || !Number.isFinite(value)) return Promise.resolve(false);
    const {min, max, step} = state.zoomRange;
    const snapped = min + Math.round((clamp(value, min, max) - min) / step) * step;
    return change(workspace, 'zoom', Number(clamp(snapped, min, max).toFixed(4)));
  }

  window.EventDeskCamera = {selectCamera, constraints, configure, reset, toggleTorch, setZoom};
})();
