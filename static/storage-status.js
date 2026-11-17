/* Request identities and last confirmed storage status contain no employee data. */
(function (global) {
  'use strict';
  let status = Object.freeze({storage:null, sync:null, offline:false, uncertain:false});
  let requestSequence = 0, lastDegradation = 0;
  const uncertainOperations = new Set();
  const listeners = new Set();

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
    const pending = payload?.pending_sync === true;
    const degraded = pending || source === 'sqlite' || ['pending','recovering'].includes(progress);
    if (degraded) lastDegradation = Math.max(lastDegradation, request);
    const current = request >= lastDegradation;
    const storage = current && ['sql','sqlite'].includes(source) ? source : status.storage;
    const sync = current && ['synced','pending','recovering'].includes(progress) ? progress : status.sync;
    const fullySynced = current && source === 'sql' && progress === 'synced' && !pending;
    if (resolvedOperation) uncertainOperations.delete(resolvedOperation);
    if (fullySynced && headers?.get('X-EventDesk-Uncertain') === 'clear') uncertainOperations.clear();
    const offline = fullySynced ? false : pending || source === 'sqlite'
      || ['pending','recovering'].includes(progress) || status.offline;
    return publish({storage:pending ? 'sqlite' : storage, sync:pending ? 'pending' : sync,
      offline, uncertain:uncertainOperations.size > 0});
  }

  function subscribe(listener) {
    listeners.add(listener);
    listener(status);
    return () => listeners.delete(listener);
  }

  global.EventDeskStorage = Object.freeze({operationId, beginRequest, markUncertain,
    update, subscribe, current:() => status});
})(window);
