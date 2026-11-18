// Private application envelopes share the photo service and its backend bearer.
// The Python backend validates inner routes, sessions, CSRF and SQL operations.
export const EVENT_LIMITS = Object.freeze({ maxRequestBytes: 12 * 1024 * 1024,
  maxResponseBytes: 24 * 1024 * 1024, deadlineMs: 30000 });

export function isEventRoute(path) {
  return path === '/v1/event/request' || path === '/v1/event/snapshot';
}

function jsonType(value) {
  return typeof value === 'string' && value.length <= 128
    && /^application\/json(?:\s*;\s*charset\s*=\s*(?:utf-8|"utf-8"))?$/i.test(value.trim());
}

function failure(request, status) {
  const errors = { 400: 'bad request', 401: 'unauthorized', 403: 'forbidden',
    404: 'not found', 405: 'method not allowed', 409: 'conflict', 413: 'request too large',
    415: 'unsupported media type', 503: 'unavailable' };
  return new Response(request.method === 'HEAD' ? null : JSON.stringify({ error: errors[status] }) + '\n', {
    status, headers: { ...safeHeaders(), ...(status === 401 ? { 'WWW-Authenticate': 'Bearer' }
      : status === 405 ? { Allow: 'POST' } : {}) },
  });
}

function safeHeaders() {
  return { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'private, no-store',
    'X-Content-Type-Options': 'nosniff', Vary: 'Authorization' };
}

function cancel(body) {
  if (body && !body.locked) void body.cancel().catch(() => {});
}

class Oversized extends Error {}
class ProtocolFailure extends Error {}

function expectedLength(headers, limit) {
  const value = headers.get('Content-Length');
  if (value === null) return null;
  if (!/^\d+$/.test(value)) throw new ProtocolFailure();
  const length = Number(value);
  if (length > limit || !Number.isSafeInteger(length)) throw new Oversized();
  return length;
}

async function readBounded(body, limit, expected, signal) {
  if (!body) throw new ProtocolFailure();
  const reader = body.getReader();
  const abort = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener('abort', abort, { once: true });
  let total = 0;
  const chunks = [];
  const decoder = new TextDecoder('utf-8', { fatal: true });
  try {
    if (signal.aborted) throw new Error('Deadline');
    while (true) {
      const { done, value } = await reader.read();
      if (signal.aborted) throw new Error('Deadline');
      if (done) break;
      if (!(value instanceof Uint8Array)) throw new ProtocolFailure();
      if (total + value.byteLength > limit) throw new Oversized();
      // Validate split UTF8 sequences without materializing a full JSON string
      // or object graph. Backend/primary Worker enforce the envelope schema.
      for (let offset = 0; offset < value.byteLength; offset += 16384) {
        try { decoder.decode(value.subarray(offset, offset + 16384), { stream: true }); }
        catch { throw new ProtocolFailure(); }
      }
      total += value.byteLength;
      chunks.push(value);
    }
    if (total === 0 || (expected !== null && total !== expected)) throw new ProtocolFailure();
    try { decoder.decode(); } catch { throw new ProtocolFailure(); }
    const bytes = new Uint8Array(total);
    let offset = 0;
    for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
    return bytes;
  } catch (error) {
    void reader.cancel().catch(() => {});
    throw error;
  } finally {
    signal.removeEventListener('abort', abort);
    reader.releaseLock();
  }
}

function configured(config) {
  return typeof config?.service?.fetch === 'function' && typeof config.backendToken === 'string'
    && config.backendToken.length >= 32 && config.backendToken.length <= 512
    && /^[\x21-\x7e]+$/.test(config.backendToken) && new Set(config.backendToken).size >= 12;
}

async function operation(request, config, signal) {
  let bytes;
  try {
    const expected = expectedLength(request.headers, EVENT_LIMITS.maxRequestBytes);
    bytes = await readBounded(request.body, EVENT_LIMITS.maxRequestBytes, expected, signal);
  } catch (error) {
    if (signal.aborted) throw error;
    cancel(request.body);
    return failure(request, error instanceof Oversized ? 413 : 400);
  }
  const upstream = await config.service.fetch(new Request(`http://127.0.0.1:8765${new URL(request.url).pathname}`, {
    method: 'POST', redirect: 'manual', signal, body: bytes,
    headers: { Authorization: `Bearer ${config.backendToken}`, Accept: 'application/json',
      'Content-Type': 'application/json; charset=utf-8' },
  }));
  try {
    if (signal.aborted) throw new Error('Deadline');
    if (upstream.redirected) throw new ProtocolFailure();
    if (upstream.status !== 200) {
      return failure(request, [400, 401, 403, 404, 405, 409, 413, 415].includes(upstream.status)
        ? upstream.status : upstream.status >= 500 ? 503 : 400);
    }
    const encoding = upstream.headers.get('Content-Encoding');
    if (!jsonType(upstream.headers.get('Content-Type')) || (encoding && encoding.toLowerCase() !== 'identity')) {
      throw new ProtocolFailure();
    }
    const expected = expectedLength(upstream.headers, EVENT_LIMITS.maxResponseBytes);
    const response = await readBounded(upstream.body, EVENT_LIMITS.maxResponseBytes, expected, signal);
    return new Response(response, { headers: { ...safeHeaders(), 'Content-Length': String(response.length) } });
  } catch (error) {
    if (!signal.aborted && (error instanceof Oversized || error instanceof ProtocolFailure)) return failure(request, 400);
    throw error;
  } finally {
    cancel(upstream.body);
  }
}

export async function proxyEventRequest(request, config, { deadlineMs = EVENT_LIMITS.deadlineMs } = {}) {
  if (!configured(config)) return failure(request, 401);
  if (!isEventRoute(new URL(request.url).pathname)) return failure(request, 404);
  if (request.method !== 'POST') return failure(request, 405);
  const encoding = request.headers.get('Content-Encoding');
  if (request.url.includes('?') || !jsonType(request.headers.get('Content-Type'))
      || (encoding && encoding.toLowerCase() !== 'identity')) return failure(request, 400);
  if (!Number.isInteger(deadlineMs) || deadlineMs < 1 || deadlineMs > EVENT_LIMITS.deadlineMs) return failure(request, 503);
  const controller = new AbortController();
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => { controller.abort(); reject(new Error('Deadline')); }, deadlineMs);
  });
  try {
    return await Promise.race([operation(request, config, controller.signal), deadline]);
  } catch {
    return failure(request, 503);
  } finally {
    clearTimeout(timer);
    controller.abort();
    cancel(request.body);
  }
}
