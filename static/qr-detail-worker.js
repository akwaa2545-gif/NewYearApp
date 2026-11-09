'use strict';

importScripts('/static/vendor/jsQR.js');

function decodeFrame(data, width, height, enhance) {
  let result = jsQR(data, width, height, {inversionAttempts:'dontInvert'});
  if (!result && enhance) {
    const adjusted = contrastFrame(data, width * height);
    if (adjusted) result = jsQR(adjusted, width, height, {inversionAttempts:'attemptBoth'});
  }
  return result?.data || null;
}

function contrastFrame(data, pixels) {
  const histogram = new Uint32Array(256);
  for (let i = 0; i < data.length; i += 4) {
    histogram[(77 * data[i] + 150 * data[i + 1] + 29 * data[i + 2]) >>> 8] += 1;
  }
  // Ignore sparse sensor-noise outliers, but retain small QR modules in a full frame.
  const tail = Math.max(1, Math.floor(pixels * 0.001));
  let low = 0, high = 255, count = 0;
  while (low < 255 && (count += histogram[low]) < tail) low += 1;
  count = 0;
  while (high > low && (count += histogram[high]) < tail) high -= 1;
  const range = high - low;
  if (range < 12 || range >= 180) return null;
  const gain = Math.min(3, 180 / range);
  const midpoint = (low + high) / 2;
  const output = new Uint8ClampedArray(data.length);
  for (let i = 0; i < data.length; i += 4) {
    const luminance = (77 * data[i] + 150 * data[i + 1] + 29 * data[i + 2]) >>> 8;
    const adjusted = Math.round(128 + (luminance - midpoint) * gain);
    output[i] = output[i + 1] = output[i + 2] = adjusted;
    output[i + 3] = 255;
  }
  return output;
}

self.onmessage = event => {
  const id = event.data?.id;
  try {
    const {data, width, height, enhance} = event.data || {};
    const pixels = width * height;
    if (!Number.isSafeInteger(width) || !Number.isSafeInteger(height) ||
        width < 1 || height < 1 || width > 1280 || height > 1280 ||
        pixels > 1280 * 1024 || !(data instanceof Uint8ClampedArray) ||
        data.length !== pixels * 4) {
      throw new Error('Invalid frame.');
    }
    const result = decodeFrame(data, width, height, enhance === true);
    const code = typeof result === 'string' && result.trim() && result.length <= 4096 ? result : null;
    self.postMessage({id, code});
  } catch (_) {
    self.postMessage({id, code: null, error: 'Unable to decode frame.'});
  }
};
