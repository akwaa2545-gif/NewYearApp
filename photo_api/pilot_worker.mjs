// Isolated private pilot. PHOTO_API is a Workers VPC service binding; SQL stays on premises.
// Required secrets: PHOTO_API_TOKEN and a distinct PILOT_ACCESS_TOKEN (32+ random characters).
// PHOTO_SCOPE='all-employees' permits canonical IDs after the app's employee-master check.
// Default/'pilot' scope requires PILOT_EMPLOYEE_IDS: exactly ten 7-digit strings.
const MAX_PHOTO_BYTES = 256 * 1024;
const MAX_HEALTH_BYTES = 4096;
const DEADLINE_MS = 8000;
const encoder = new TextEncoder();

function validSecret(value) {
  return typeof value === 'string' && value.length >= 32 && value.length <= 512
    && /^[\x21-\x7e]+$/.test(value) && new Set(value).size >= 12;
}

function configuration(env) {
  if (!env || typeof env.PHOTO_API?.fetch !== 'function'
    || !validSecret(env.PHOTO_API_TOKEN) || !validSecret(env.PILOT_ACCESS_TOKEN)
    || env.PHOTO_API_TOKEN === env.PILOT_ACCESS_TOKEN) {
    throw new Error('Invalid configuration');
  }
  const scope = env.PHOTO_SCOPE === undefined ? 'pilot' : env.PHOTO_SCOPE;
  if (scope !== 'pilot' && scope !== 'all-employees') throw new Error('Invalid photo scope');
  let ids = null;
  if (scope === 'pilot') {
    if (typeof env.PILOT_EMPLOYEE_IDS !== 'string' || env.PILOT_EMPLOYEE_IDS.length > 256) {
      throw new Error('Invalid pilot');
    }
    ids = JSON.parse(env.PILOT_EMPLOYEE_IDS);
    if (!Array.isArray(ids) || ids.length !== 10 || new Set(ids).size !== 10
      || ids.some(id => typeof id !== 'string' || !/^\d{7}$/.test(id))) {
      throw new Error('Invalid pilot');
    }
  }
  return { service: env.PHOTO_API, backendToken: env.PHOTO_API_TOKEN,
    accessToken: env.PILOT_ACCESS_TOKEN, ids: ids === null ? null : new Set(ids) };
}

async function authorized(request, expected) {
  const supplied = request.headers.get('Authorization') || '';
  const match = /^Bearer ([\x21-\x7e]{1,512})$/i.exec(supplied);
  const hashes = await Promise.all([match?.[1] || '', expected].map(value =>
    crypto.subtle.digest('SHA-256', encoder.encode(value))));
  const left = new Uint8Array(hashes[0]);
  const right = new Uint8Array(hashes[1]);
  let difference = 0;
  for (let index = 0; index < left.length; index++) difference |= left[index] ^ right[index];
  return difference === 0 && match !== null;
}

function safeHeaders(contentType = 'application/json; charset=utf-8') {
  return { 'Content-Type': contentType, 'Cache-Control': 'private, no-store',
    'X-Content-Type-Options': 'nosniff', Vary: 'Authorization' };
}

function jsonResponse(request, status, value, extra = {}) {
  return new Response(request.method === 'HEAD' ? null : JSON.stringify(value) + '\n', {
    status, headers: { ...safeHeaders(), ...extra },
  });
}

function failure(request, status) {
  const errors = { 400: 'bad request', 401: 'unauthorized', 404: 'not found',
    405: 'method not allowed', 503: 'unavailable' };
  const headers = status === 401 ? { 'WWW-Authenticate': 'Bearer' }
    : status === 405 ? { Allow: 'GET, HEAD' } : {};
  return jsonResponse(request, status, { error: errors[status] }, headers);
}

function route(request, ids) {
  const url = new URL(request.url);
  if (request.url.includes('?') || request.body !== null) return { error: 400 };
  if (url.pathname === '/healthz' || url.pathname === '/readyz') {
    return { path: url.pathname, kind: 'health', status: url.pathname === '/healthz' ? 'ok' : 'ready' };
  }
  const match = /^\/v1\/employees\/(.*)\/photo$/.exec(url.pathname);
  if (!match) return { error: 404 };
  if (!/^\d{7}$/.test(match[1])) return { error: 400 };
  if (ids !== null && !ids.has(match[1])) return { error: 404 };
  return { path: `/v1/employees/${match[1]}/photo`, kind: 'photo' };
}

// Cancellation is cleanup only; failures still return a generic unavailable response.
function cancelBody(body) {
  if (body && !body.locked) void body.cancel().catch(() => {});
}

async function boundedBytes(body, limit, signal) {
  if (!body) throw new Error('Missing body');
  const reader = body.getReader();
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  const chunks = [];
  let total = 0;
  try {
    if (signal.aborted) throw new Error('Deadline');
    while (true) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error('Deadline');
      if (done) break;
      if (!(value instanceof Uint8Array) || total + value.byteLength > limit) {
        void reader.cancel().catch(() => {});
        throw new Error('Invalid body');
      }
      total += value.byteLength;
      chunks.push(value);
    }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  } finally {
    signal.removeEventListener('abort', abort);
    reader.releaseLock();
  }
}

function healthType(contentType) {
  return /^(application\/json|text\/plain)(;\s*charset=utf-8)?$/i.test(contentType || '');
}

function validateHealth(bytes, expected, type) {
  const text = new TextDecoder('utf-8', { fatal: true }).decode(bytes);
  if (type.toLowerCase().startsWith('application/json')) {
    const value = JSON.parse(text);
    if (value?.status !== expected) throw new Error('Invalid health');
  } else if (text !== expected + '\n') throw new Error('Invalid health');
}

async function upstreamResponse(request, config, selected, signal) {
  // Construct a new request from constants; never forward client headers or destinations.
  const upstream = await config.service.fetch(new Request(`http://127.0.0.1:8765${selected.path}`, {
    method: request.method, redirect: 'manual', signal,
    headers: { Authorization: `Bearer ${config.backendToken}`,
      Accept: selected.kind === 'photo' ? 'image/jpeg' : 'application/json, text/plain' },
  }));
  if (signal.aborted) { cancelBody(upstream.body); throw new Error('Deadline'); }
  if (upstream.status !== 200 || upstream.redirected) {
    cancelBody(upstream.body);
    return failure(request, upstream.status === 404 && selected.kind === 'photo' ? 404 : 503);
  }
  const photo = selected.kind === 'photo';
  const type = upstream.headers.get('Content-Type');
  const limit = photo ? MAX_PHOTO_BYTES : MAX_HEALTH_BYTES;
  if (photo ? type !== 'image/jpeg' : !healthType(type)) {
    cancelBody(upstream.body);
    throw new Error('Invalid type');
  }
  if (request.method === 'HEAD') {
    cancelBody(upstream.body);
    const length = upstream.headers.get('Content-Length');
    if (!/^\d+$/.test(length || '') || Number(length) < (photo ? 3 : 1) || Number(length) > limit) {
      throw new Error('Invalid length');
    }
    return photo ? new Response(null, { headers: { ...safeHeaders('image/jpeg'), 'Content-Length': length } })
      : jsonResponse(request, 200, { status: selected.status });
  }
  const bytes = await boundedBytes(upstream.body, limit, signal);
  if (!photo) {
    validateHealth(bytes, selected.status, type);
    return jsonResponse(request, 200, { status: selected.status });
  }
  if (bytes.length < 3 || bytes[0] !== 255 || bytes[1] !== 216 || bytes[2] !== 255) {
    throw new Error('Invalid JPEG');
  }
  return new Response(bytes, { headers: { ...safeHeaders('image/jpeg'), 'Content-Length': String(bytes.length) } });
}

async function proxy(request, config, selected) {
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error('Deadline')); }, DEADLINE_MS);
  });
  try {
    return await Promise.race([upstreamResponse(request, config, selected, controller.signal), deadline]);
  } finally {
    clearTimeout(timer);
  }
}

export default {
  async fetch(request, env) {
    try {
      const config = configuration(env);
      if (!await authorized(request, config.accessToken)) return failure(request, 401);
      if (request.method !== 'GET' && request.method !== 'HEAD') return failure(request, 405);
      const selected = route(request, config.ids);
      if (selected.error) return failure(request, selected.error);
      return await proxy(request, config, selected);
    } catch {
      // Never return backend payloads, exception text, hostnames or secret values.
      return failure(request, 503);
    }
  },
};
