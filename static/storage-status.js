/* Request identities and last confirmed storage status contain no employee data. */
(function (global) {
  'use strict';
  let status = Object.freeze({storage:null, sync:null, backup:null, queue:null, offline:false, uncertain:false});
  let requestSequence = 0, lastDegradation = 0;
  let backupProbeAt = 0, backupProbeInFlight = false;
  const uncertainOperations = new Set();
  const listeners = new Set();
  const identities = new Map();
  const observedIdentities = new Map();
  const prefix = 'eventdesk.pending.v1.';
  const maximumIdentities = 256;
  let persistent = true;

  // Keep unresolved IDs indefinitely: expiring one could turn a retry into another draw.
  function restoreIdentities() {
    let storage;
    try { storage = global.localStorage; if (storage) void storage.length; }
    catch (_) { persistent = false; return; }
    if (!storage) { persistent = false; return; }
    try {
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index);
        if (!key?.startsWith(prefix) || !/^[a-f0-9]{64}$/.test(key.slice(prefix.length))) continue;
        const saved = storage.getItem(key);
        let entry;
        try {
          entry = JSON.parse(saved);
          if (!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(entry?.id) || !Number.isFinite(entry.created)) {
            throw new Error('Invalid saved request');
          }
        } catch (_) {
          // A damaged identity record cannot safely authorize a replacement command.
          uncertainOperations.add('saved-request-unreadable');
          persistent = false;
          return;
        }
        if (identities.get(key)?.id !== entry.id) {
          // A newer command in another tab must not replace a local retry identity.
          const restored = {id:entry.id, created:entry.created, saved:true};
          if (!identities.has(key)) {
            identities.set(key, restored);
            observedIdentities.delete(entry.id);
          } else observedIdentities.set(entry.id, {key, entry:restored});
          uncertainOperations.add(entry.id);
        }
      }
    } catch (_) {
      persistent = false;
    }
  }

  async function prepareOperation(requestKey) {
    let key = requestKey;
    if (global.crypto?.subtle && global.TextEncoder) {
      const digest = await global.crypto.subtle.digest('SHA-256', new global.TextEncoder().encode(requestKey));
      key = prefix + Array.from(new Uint8Array(digest), value => value.toString(16).padStart(2, '0')).join('');
    } else persistent = false;
    const prepare = () => {
      // Merge records from other tabs before deciding whether a new command is safe.
      if (persistent) {
        restoreIdentities();
        publish({...status, uncertain:uncertainOperations.size > 0});
      }
      const existing = identities.get(key);
      if (existing) return existing.id;
      if (uncertainOperations.size) throw new Error('Last save is not confirmed. Retry the same action before making another change.');
      if (identities.size + observedIdentities.size >= maximumIdentities) throw new Error('Too many saves are waiting for confirmation. Retry a pending action first.');
      const entry = {id:operationId(), created:Date.now()};
      identities.set(key, entry);
      if (persistent) {
        try {
          global.localStorage.setItem(key, JSON.stringify(entry));
          identities.set(key, {...entry, saved:true});
        }
        catch (_) { persistent = false; }
      }
      return entry.id;
    };
    if (global.navigator?.locks?.request) return global.navigator.locks.request('eventdesk-request-identity', prepare);
    return prepare();
  }

  function resolveOperation(id) {
    for (const [key, entry] of identities) {
      if (entry.id !== id) continue;
      if (entry.saved) {
        try {
          const saved = global.localStorage?.getItem(key);
          if (saved && JSON.parse(saved).id === id) global.localStorage.removeItem(key);
        }
        catch (_) { markUncertain(id); return false; }
      }
      identities.delete(key);
    }
    observedIdentities.delete(id);
    uncertainOperations.delete(id);
    // Keep observed IDs retryable even if another tab has already removed its record.
    for (const [observedId, observed] of observedIdentities) {
      if (!identities.has(observed.key)) {
        identities.set(observed.key, observed.entry);
        observedIdentities.delete(observedId);
      }
    }
    if (persistent) restoreIdentities();
    return true;
  }

  async function completeOperation(id) {
    const complete = () => resolveOperation(id);
    if (global.navigator?.locks?.request) return global.navigator.locks.request('eventdesk-request-identity', complete);
    return complete();
  }

  function operationId() {
    const provider = global.crypto;
    if (typeof provider?.randomUUID === 'function') return provider.randomUUID();
    if (typeof provider?.getRandomValues !== 'function') {
      throw new Error('Your browser cannot create a secure request ID.');
    }
    const bytes = provider.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`;
  }

  function beginRequest() { return ++requestSequence; }

  function publish(next) {
    if (next.storage === status.storage && next.sync === status.sync
        && next.backup === status.backup && next.queue === status.queue
        && next.offline === status.offline && next.uncertain === status.uncertain) return status;
    status = Object.freeze(next);
    for (const listener of listeners) listener(status);
    return status;
  }

  function markUncertain(operation, observation) {
    if (!operation) return;
    uncertainOperations.add(operation);
    lastDegradation = Math.max(lastDegradation, observation || beginRequest());
    publish({...status, uncertain:true});
  }

  function update(headers, payload, observation, resolvedOperation) {
    const request = observation || beginRequest();
    const source = headers?.get('X-EventDesk-Storage');
    const progress = headers?.get('X-EventDesk-Sync');
    const backupProgress = headers?.get('X-EventDesk-Backup');
    const queueProgress = headers?.get('X-EventDesk-Queue');
    const pending = payload?.pending_sync === true;
    const degraded = pending || source === 'sqlite' || ['pending','recovering'].includes(progress);
    if (degraded) lastDegradation = Math.max(lastDegradation, request);
    const current = request >= lastDegradation;
    const storage = current && ['sql','sqlite'].includes(source) ? source : status.storage;
    const sync = current && (['synced','pending','recovering'].includes(progress)
      || source === 'sql' && progress === 'online') ? progress : status.sync;
    const backup = current && ['current','stale','unavailable'].includes(backupProgress) ? backupProgress : status.backup;
    const fullySynced = current && source === 'sql' && progress === 'synced' && !pending;
    const online = current && source === 'sql' && progress === 'online' && !pending;
    // Direct SQL proves connectivity, not that the separate outbox is empty.
    // The legacy coordinator's synced response remains compatible; an explicit
    // unknown/pending queue never upgrades a known pending check-in to clear.
    const queue = pending || current && queueProgress === 'pending' ? 'pending'
      : fullySynced && (queueProgress == null || queueProgress === 'clear') ? 'clear'
      : current && queueProgress === 'unknown' && status.queue !== 'pending' ? 'unknown' : status.queue;
    if (resolvedOperation) resolveOperation(resolvedOperation);
    const offline = fullySynced || online ? false : pending || source === 'sqlite'
      || ['pending','recovering'].includes(progress) || status.offline;
    return publish({storage:pending ? 'sqlite' : storage, sync:pending ? 'pending' : sync, backup, queue,
      offline, uncertain:uncertainOperations.size > 0});
  }

  function subscribe(listener) {
    listeners.add(listener);
    listener(status);
    return () => listeners.delete(listener);
  }

  async function confirmBackup(active) {
    if (active !== true || status.queue !== 'pending' || status.storage !== 'sql'
        || status.offline || backupProbeInFlight || Date.now() < backupProbeAt
        || typeof global.fetch !== 'function') return false;
    backupProbeAt = Date.now() + 60000;
    backupProbeInFlight = true;
    const observation = beginRequest();
    const controller = typeof global.AbortController === 'function' ? new global.AbortController() : null;
    const timer = controller && global.setTimeout && global.clearTimeout
      ? global.setTimeout(() => controller.abort(), 8000) : null;
    let response;
    try {
      response = await global.fetch('/api/me?backup=1', {method:'GET', credentials:'same-origin',
        cache:'no-store', ...(controller ? {signal:controller.signal} : {})});
      if (!response.ok || response.headers.get('X-EventDesk-Storage') !== 'sql'
          || response.headers.get('X-EventDesk-Sync') !== 'synced'
          || response.headers.get('X-EventDesk-Queue') !== 'clear') return false;
      update(response.headers, null, observation);
      return status.queue === 'clear';
    } catch (_) { return false; }
    finally {
      if (timer !== null) global.clearTimeout(timer);
      backupProbeInFlight = false;
      try { await response?.body?.cancel?.(); } catch (_) { /* The status headers are sufficient. */ }
    }
  }

  restoreIdentities();
  status = Object.freeze({...status, uncertain:uncertainOperations.size > 0});
  global.addEventListener?.('storage', event => {
    if (event.key && !event.key.startsWith(prefix)) return;
    if (event.newValue) restoreIdentities();
    // Deletion in another tab cannot prove a local in-flight request has resolved.
    publish({...status, uncertain:uncertainOperations.size > 0});
  });
  global.EventDeskStorage = Object.freeze({operationId, prepareOperation, completeOperation, beginRequest, markUncertain,
    update, subscribe, confirmBackup, current:() => status});
})(window);
