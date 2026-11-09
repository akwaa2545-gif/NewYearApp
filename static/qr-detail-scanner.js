'use strict';

// Keep extra decoding off the UI thread and independent of the camera's preview size.
(function (global) {
  function start(workspace, onCode, generation) {
    if (!global.Worker || typeof document === 'undefined') return () => {};
    let canvas, context, worker;
    try {
      canvas = document.createElement('canvas');
      context = canvas.getContext('2d', {willReadFrequently:true});
      if (!context) return () => {};
      worker = new global.Worker('/static/qr-detail-worker.js');
    } catch (_) {
      return () => {};
    }
    let stopped = false, timer = null, timeout = null, pending = false, frame = 0, job = 0, frameSequence = 0;
    let cadence = 250, frameStartedAt = 0;
    const current = () => !stopped && workspace.cameraGeneration === generation && workspace.scanner?.isScanning;
    const available = () => current() && !document.hidden && !workspace.scanBusy &&
      !workspace.lookupPending && !workspace.actionPending && Date.now() - (workspace.lastScanAt || 0) >= 1500;
    function stop() {
      if (stopped) return;
      stopped = true;
      global.clearTimeout(timer);
      global.clearTimeout(timeout);
      worker.terminate();
      canvas.width = canvas.height = 0;
    }
    function unavailable() {
      if (current() && workspace.cameraHint) {
        workspace.cameraHint.textContent = 'Using normal scan.';
      }
      stop();
    }
    function schedule() {
      if (current()) timer = global.setTimeout(scan, cadence);
      else stop();
    }
    function scan() {
      timer = null;
      if (!current()) return stop();
      if (!available() || pending) return schedule();
      const video = workspace.reader.querySelector?.('video');
      if (!video || video.readyState < 2 || video.paused || video.ended ||
          !video.videoWidth || !video.videoHeight) return schedule();
      const phase = frame % 3;
      const full = phase === 2;
      // Keep tiny modules at their native size; the overview still covers codes near the edges.
      const width = full ? video.videoWidth : Math.min(video.videoWidth, video.videoHeight, phase === 0 ? 640 : 1024);
      const height = full ? video.videoHeight : width;
      const scale = Math.min(1, (full ? 1280 : 1024) / Math.max(width, height),
        Math.sqrt((1280 * 1024) / (width * height)));
      canvas.width = Math.max(1, Math.floor(width * scale));
      canvas.height = Math.max(1, Math.floor(height * scale));
      try {
        frameStartedAt = Date.now();
        context.drawImage(video, Math.floor((video.videoWidth - width) / 2), Math.floor((video.videoHeight - height) / 2),
          width, height, 0, 0, canvas.width, canvas.height);
        const image = context.getImageData(0, 0, canvas.width, canvas.height);
        pending = true;
        frameSequence = workspace.scanSequence || 0;
        worker.postMessage({id:++job, data:image.data, width:canvas.width, height:canvas.height,
          enhance:phase === 1}, [image.data.buffer]);
        frame += 1;
        timeout = global.setTimeout(unavailable, 5000);
      } catch (_) {
        unavailable();
      }
    }
    worker.onmessage = async event => {
      if (stopped || !pending || event.data?.id !== job) return;
      global.clearTimeout(timeout);
      if (!current()) return stop();
      if (event.data.error) return unavailable();
      // Measure capture/transfer/decode time, excluding any employee lookup awaited below.
      cadence = Math.min(1000, Math.max(250, Math.ceil((Date.now() - frameStartedAt) * 1.5)));
      try {
        const code = event.data.code;
        if (available() && (workspace.scanSequence || 0) === frameSequence &&
            typeof code === 'string' && code.trim() && code.length <= 4096) await onCode(code);
      } catch (_) {
        return unavailable();
      }
      pending = false;
      schedule();
    };
    worker.onerror = unavailable;
    schedule();
    return stop;
  }
  global.EventDeskDetailScanner = {start};
})(window);
