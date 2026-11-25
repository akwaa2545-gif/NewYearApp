'use strict';

window.EventDeskProjectorAudio = (() => {
  const volumeKey = 'eventdesk-projector-volumes';
  const muteKey = 'eventdesk-projector-sound';
  const defaults = {music: .38, effects: .72};
  const assets = {music:'festival-loop', roll:'drum-roll', lock:'digit-lock', victory:'victory'};

  function readVolumes() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(volumeKey)) || {};
      return Object.fromEntries(Object.entries(defaults).map(([key, fallback]) =>
        [key, Number.isFinite(saved[key]) ? Math.max(0, Math.min(1, saved[key])) : fallback]));
    } catch (_) { return {...defaults}; }
  }

  function saveVolumes(volumes) {
    try { window.localStorage.setItem(volumeKey, JSON.stringify(volumes)); }
    catch (_) { /* Playback can continue with the current mix. */ }
  }

  function create(onStatus) {
    const Audio = window.AudioContext || window.webkitAudioContext;
    const buffers = new Map(), cues = new Set();
    let context, musicGain, effectsGain, duckGain;
    let music, roll, victory;
    let active = false, mode = 'idle', enabled = true;
    let volumes = readVolumes();
    try { enabled = window.localStorage.getItem(muteKey) !== 'off'; } catch (_) {}

    function ramp(parameter, value, seconds) {
      parameter.setTargetAtTime(value, context.currentTime, seconds);
    }

    function play(key, output, {loop = false, rate = 1} = {}) {
      if (!active || !enabled || context?.state !== 'running' || !buffers.has(key)) return null;
      const source = context.createBufferSource();
      source.buffer = buffers.get(key);
      source.loop = loop;
      source.playbackRate.value = rate;
      source.connect(output);
      source.start();
      return source;
    }

    function stop(source) {
      if (!source) return;
      source.onended = null;
      source.stop();
      source.disconnect();
    }

    function sync() {
      if (!active || !Audio) return;
      const running = context?.state === 'running';
      onStatus(enabled ? (running ? 'enabled' : 'blocked') : 'muted');
      if (!running || !enabled) return;
      if (!music) music = play('music', duckGain, {loop:true});
      if (!roll && (mode === 'drawing' || mode === 'final')) {
        roll = play('roll', effectsGain, {loop:true, rate:mode === 'final' ? 1.29 : 1});
      }
    }

    function open() {
      active = true;
      if (!Audio) return;
      if (!context) {
        try { context = new Audio(); }
        catch (_) { return; }
        musicGain = context.createGain();
        effectsGain = context.createGain();
        duckGain = context.createGain();
        duckGain.connect(musicGain).connect(context.destination);
        effectsGain.connect(context.destination);
        musicGain.gain.value = volumes.music;
        effectsGain.gain.value = volumes.effects;
        duckGain.gain.value = mode === 'idle' ? 1 : .18;
        context.addEventListener('statechange', sync);
        const loadingContext = context;
        for (const [key, file] of Object.entries(assets)) {
          fetch('/static/assets/audio/' + file + '.wav')
            .then(response => {
              if (!response.ok) throw new Error('Audio unavailable');
              return response.arrayBuffer();
            })
            .then(bytes => loadingContext.decodeAudioData(bytes))
            .then(buffer => { buffers.set(key, buffer); sync(); })
            .catch(() => {}); // A missing clip must never block a draw.
        }
      }
      reloadVolumes();
      sync();
      if (enabled) unlock();
    }

    function unlock() {
      if (!active || !enabled || !context || context.state === 'running') return;
      void context.resume().then(sync, sync);
    }

    function clearCues() {
      stop(roll);
      stop(victory);
      cues.forEach(stop);
      cues.clear();
      roll = victory = null;
    }

    function reset() {
      if (mode === 'idle' && !cues.size) return;
      mode = 'idle';
      clearCues();
      if (context) ramp(duckGain.gain, 1, .25);
    }

    function startDraw() {
      clearCues();
      mode = 'drawing';
      if (context) ramp(duckGain.gain, .18, .11);
      sync();
    }

    function reelTick({index, phase}) {
      if (phase !== 'stop' || (mode !== 'drawing' && mode !== 'final')) return;
      if (index === 5) {
        mode = 'final';
        if (roll) ramp(roll.playbackRate, 1.29, .42);
        if (context) ramp(duckGain.gain, .08, .15);
      }
      const chime = play('lock', effectsGain, {rate:2 ** ((index + (index === 6 ? 3 : 0)) / 12)});
      if (chime) {
        cues.add(chime);
        chime.onended = () => { cues.delete(chime); chime.disconnect(); };
      }
    }

    function finishDraw() {
      if (mode !== 'drawing' && mode !== 'final') return;
      stop(roll);
      roll = null;
      mode = 'settled';
    }

    function celebrate() {
      finishDraw();
      if (mode === 'celebrating') return;
      victory = play('victory', effectsGain);
      mode = victory ? 'celebrating' : 'idle';
      if (context) ramp(duckGain.gain, victory ? .1 : 1, .19);
      if (victory) {
        const source = victory;
        source.onended = () => {
          source.disconnect();
          victory = null;
          mode = 'idle';
          ramp(duckGain.gain, 1, .36);
        };
      }
    }

    function stopPlayback() {
      if (mode === 'celebrating') reset();
      clearCues();
      stop(music);
      music = null;
    }

    function close() {
      active = false;
      stopPlayback();
    }

    function toggle() {
      enabled = !enabled;
      try { window.localStorage.setItem(muteKey, enabled ? 'on' : 'off'); } catch (_) {}
      if (!enabled) stopPlayback();
      else unlock();
      sync();
    }

    function reloadVolumes() {
      volumes = readVolumes();
      if (!context) return;
      ramp(musicGain.gain, volumes.music, .045);
      ramp(effectsGain.gain, volumes.effects, .045);
    }

    return {open, close, unlock, toggle, reloadVolumes, startDraw, reelTick, finishDraw, celebrate, reset};
  }

  return {create, readVolumes, saveVolumes, volumeKey};
})();
