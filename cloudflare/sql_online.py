"""SQL-authoritative routing with a throttled, optional SQLite fallback.

Replica failures only affect offline service. Ordinary SQL commands neither
inspect nor write local storage; recovery uses the existing receipt/proof rules.
"""
import base64
from datetime import datetime, timezone
import json
import time
import uuid

from .event_primary import (MAX_CHECKIN_BODY, READ_POSTS, PrimaryProtocolError,
                            PrimaryUnavailable, SQLPrimary, _captured, _failure, _integer, _json)
from .replica import intent_hash


class SQLOnlinePrimary(SQLPrimary):
    MAINTENANCE_SECONDS = 60
    STORAGE_COOLDOWN_SECONDS = 300

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cached_state = None
        self.cached_queue = ()
        self.queue_known = False
        self.backup_current = False
        self.storage_retry_at = 0
        self.maintenance_at = 0
        self.latest_revision = 0
        self.recovery_refresh = False

    def _use(self, action):
        """No local exception may override an authoritative SQL response."""
        if time.monotonic() < self.storage_retry_at:
            return None
        try:
            value = self.atomic(action)
            self.backup_available = True
            return value
        except Exception:
            self.storage_retry_at = time.monotonic() + self.STORAGE_COOLDOWN_SECONDS
            self.backup_available = False
            self.backup_current = False
            self.queue_known = False
            self.diagnostic('event_backup_unavailable')
            return None

    def _state(self):
        return self.cached_state if self.backup_available else None

    def _discover(self):
        def inspect(store):
            state, queue = store.state(), store.pending_metadata(self.max_replay + 1)
            if (not isinstance(state, dict) or not _integer(state.get('revision'))
                    or any(type(state.get(key)) is not bool for key in ('seeded', 'ready'))
                    or not isinstance(queue, list)
                    or any(not isinstance(item, dict) or not isinstance(item.get('operation_id'), str)
                           or item.get('kind') not in ('checkin', 'uncertain')
                           or item.get('state') not in ('pending', 'conflict') for item in queue)):
                raise ValueError('Invalid local replica metadata.')
            return state, queue
        value = self._use(inspect)
        if value is not None:
            self.cached_state, queued = value
            self.cached_queue = tuple(queued)
            self.queue_known = True
            self.backup_current = bool(self.cached_state['seeded'] and self.cached_state['ready']
                                       and self.cached_state['revision'] >= self.latest_revision)

    @staticmethod
    def _load_saved(store, operation_id):
        saved = store.find(operation_id)
        if saved is None:
            return None
        envelope = saved['envelope']
        if (not isinstance(envelope, dict) or envelope.get('operation_id') != operation_id
                or envelope.get('method') != 'POST' or not isinstance(envelope.get('path'), str)
                or not envelope['path'].startswith('/api/')
                or not isinstance(envelope.get('body_base64'), str)
                or not isinstance(envelope.get('headers'), list)):
            raise ValueError('Invalid saved operation.')
        parsed_id = uuid.UUID(operation_id)
        if parsed_id.version != 4 or str(parsed_id) != operation_id:
            raise ValueError('Invalid saved operation ID.')
        base64.b64decode(envelope['body_base64'], validate=True)
        intent_hash(envelope)
        return saved

    async def _request(self, envelope):
        message = {**envelope, 'after_revision': self.cached_state['revision'] if self.cached_state else 0,
                   'include_replica': False}
        return self._wire(message, await self.transport('request', message))

    def _decorate(self, result, storage, *, pending=False, allow_clear=False):
        headers = [(name, value) for name, value in result['headers']
                   if not name.lower().startswith('x-eventdesk-')]
        backup = ('unavailable' if not self.backup_available else
                  'current' if self.backup_current else 'stale')
        queue = ('unknown' if not self.queue_known else
                 'pending' if self.cached_queue else 'clear')
        headers.extend([('X-EventDesk-Storage', storage),
                        ('X-EventDesk-Sync', 'pending' if pending else 'synced'),
                        ('X-EventDesk-Backup', backup), ('X-EventDesk-Queue', queue)])
        if self.backup_available and self.cached_state and self.cached_state['seeded']:
            headers.append(('X-EventDesk-Backup-Revision', str(self.cached_state['revision'])))
        return {**result, 'headers': headers}

    async def _drain(self, current_id):
        """Drain known intents before a conflicting SQL write, preserving fences."""
        self.conflict_pending = False
        if not self.queue_known or not self.backup_available:
            return True  # SQL remains available when the queue cannot be inspected.
        for item in self.cached_queue[:self.max_replay]:
            if item['state'] == 'conflict':
                self.conflict_pending = True
                return False
            if item['kind'] == 'uncertain' and item['operation_id'] == current_id:
                continue
            def load(store):
                saved = self._load_saved(store, item['operation_id'])
                envelope = saved['envelope']
                if item['kind'] == 'checkin':
                    return {**envelope, 'replay': True, 'queue_proof': self._proof(envelope)}
                intent_hash(envelope)
                return {**envelope, 'receipt_only': True, 'cancel_if_missing': True}
            envelope = self._use(load)
            if envelope is None:
                return True
            result = await self._request(envelope)
            if item['kind'] == 'uncertain' and not (result.get('receipt_found') is True
                                                    or result.get('cancelled') is True):
                raise PrimaryProtocolError('The uncertain request has no verified SQL receipt.')
            if item['kind'] == 'checkin' and result['status'] >= 400:
                self._use(lambda store: store.conflict(item['operation_id']))
                self.conflict_pending = True
                return False
            def acknowledge(store):
                store.acknowledge(item['operation_id'])
                store.invalidate()
                return True
            if self._use(acknowledge) is not True:
                return True  # Keep the known intent in memory; its SQL receipt is durable.
            self.backup_current = False
            self.cached_state = {**self.cached_state, 'ready': False}
            self.cached_queue = tuple(entry for entry in self.cached_queue
                                      if entry['operation_id'] != item['operation_id'])
            self.latest_revision = max(self.latest_revision, result['revision'])
            self.recovery_refresh = True
        self._discover()  # A truncated batch must never hide later durable intents.
        return not any(item['operation_id'] != current_id or item['kind'] != 'uncertain'
                       for item in self.cached_queue)

    async def _maintain(self, envelope):
        if not self.backup_available or not self.queue_known or self.cached_queue:
            return
        try:
            message = {**envelope, 'method': 'GET', 'path': '/api/me', 'body_base64': '',
                       'operation_id': str(uuid.uuid4()), 'include_replica': True,
                       'after_revision': self.cached_state['revision'] if self.cached_state else 0}
            for flag in ('replay', 'queue_proof', 'receipt_only', 'cancel_if_missing', 'recorded_at'):
                message.pop(flag, None)
            value = self._wire(message, await self.transport('request', message))
            if self._sync(value) is not True:
                return
            state = self._use(lambda store: store.state())
            if state is None:
                return
            if value.get('snapshot_required') or not state['seeded'] or not state['ready']:
                await self._bootstrap()
            self._discover()
        except Exception:
            self.backup_current = False
            self.diagnostic('event_backup_refresh_failed')

    async def _offline(self, envelope):
        if (not self.backup_available or not self.cached_state
                or not self.cached_state['seeded'] or not self.cached_state['ready']):
            return self._decorate(_failure('SQL is unavailable and no current offline copy is ready.'),
                                  'sqlite', pending=True)
        try:
            method, path = envelope['method'], envelope['path'].split('?', 1)[0]
            if self.conflict_pending and method == 'POST' and path not in READ_POSTS:
                return self._decorate(_failure('A pending check-in needs staff review before more changes.'),
                                      'sqlite', pending=True)
            if method == 'POST' and path == '/api/scan/checkin':
                saved = self._use(lambda store: self._load_saved(store, envelope['operation_id']))
                if not self.backup_available:
                    return self._decorate(_failure('Check-in not confirmed. Retry the same action when connected.'),
                                          'sqlite', pending=True)
                result = (_captured(saved['response']) if saved and saved['kind'] == 'checkin'
                          else await self.fallback(envelope, 'checkin'))
                if 200 <= result['status'] < 300:
                    payload = {**json.loads(result['body']), 'pending_sync': True, 'storage': 'sqlite'}
                    result = {**result, 'status': 202, 'body': _json(payload).encode(),
                              'headers': [(name, value) for name, value in result['headers']
                                          if name.lower() != 'content-length']}
                self._discover()
                return self._decorate(result, 'sqlite', pending=True)
            if method == 'GET' or method == 'POST' and path in READ_POSTS:
                return self._decorate(await self.fallback(envelope, None), 'sqlite', pending=True)
            return self._decorate(_failure('SQL is unavailable. Draws and changes are paused until it reconnects.'),
                                  'sqlite', pending=True)
        except Exception:
            # Fallback check-ins are only accepted after their atomic commit succeeds.
            self.backup_available = False
            self.backup_current = False
            self.queue_known = False
            self.storage_retry_at = time.monotonic() + self.STORAGE_COOLDOWN_SECONDS
            return self._decorate(_failure('Check-in not confirmed. Retry the same action when connected.'),
                                  'sqlite', pending=True)

    async def _handle(self, envelope):
        if not isinstance(envelope, dict) or envelope.get('method') not in ('GET', 'POST'):
            raise PrimaryProtocolError('Invalid event request.')
        envelope = {**envelope, 'headers': [list(pair) for pair in envelope.get('headers', [])]}
        method, path = envelope['method'], envelope['path'].split('?', 1)[0]
        if path == '/api/scan/checkin' and len(envelope.get('body_base64', '')) > 4 * ((MAX_CHECKIN_BODY + 2) // 3):
            return _failure('The check-in request is too large.', 413)
        # This coordinator owns local queue changes. Once discovered, its cached
        # metadata stays valid until a write, recovery, or storage failure.
        if not self.queue_known:
            self._discover()
        tracked = method == 'POST' and path not in READ_POSTS
        saved = next((item for item in self.cached_queue
                      if item['operation_id'] == envelope.get('operation_id')), None)
        if tracked and saved:
            def load(store):
                item = self._load_saved(store, envelope.get('operation_id'))
                if item is not None:
                    intent_hash(item['envelope'])
                return item
            saved = self._use(load)
            if saved is not None:
                if intent_hash(saved['envelope']) != intent_hash(envelope):
                    return self._decorate(_failure('This request ID belongs to another action.', 409), 'sqlite', pending=True)
                envelope = saved['envelope']
        elif tracked and path == '/api/scan/checkin' and 'recorded_at' not in envelope:
            envelope = {**envelope, 'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            if self.cached_queue and not await self._drain(envelope.get('operation_id')) and tracked:
                return self._decorate(_failure('Pending changes need to sync before more changes.'), 'sqlite', pending=True)
            result = await self._request(envelope)
        except PrimaryUnavailable:
            if tracked and path != '/api/scan/checkin' and not saved:
                def remember(store):
                    store.enqueue(envelope, 'uncertain', {'status': 503})
                    store.invalidate()
                    return True
                if self._use(remember) is True:
                    self._discover()
            return await self._offline(envelope)
        self.latest_revision = max(self.latest_revision, result['revision'])
        if self.cached_state is None or result['revision'] > self.cached_state['revision']:
            self.backup_current = False
        if saved:
            def acknowledge(store):
                store.acknowledge(envelope['operation_id'])
                store.invalidate()
                return True
            if self._use(acknowledge) is True:
                self.cached_queue = tuple(item for item in self.cached_queue
                                          if item['operation_id'] != envelope['operation_id'])
                self.backup_current = False
                self.cached_state = {**self.cached_state, 'ready': False}
        # Ordinary SQL replies report the authoritative head without replica I/O.
        # Elapsed time alone is never a reason to inspect or rewrite SQLite.
        due = (not self.backup_current and time.monotonic() >= self.maintenance_at
               and time.monotonic() >= self.storage_retry_at)
        if due or self.recovery_refresh:
            self.maintenance_at = time.monotonic() + self.MAINTENANCE_SECONDS
            await self._maintain(envelope)
            self.recovery_refresh = False
        response = self._decorate(_captured(result), 'sql')
        if method == 'POST':
            response['headers'].append(('X-EventDesk-Operation-Resolved', envelope['operation_id']))
        return response

    async def bootstrap(self):
        try:
            revision = await super().bootstrap()
        except Exception:
            # Snapshot refresh may have invalidated SQLite before failing. The
            # next request must rediscover it even if SQL's head is unchanged.
            self.cached_state = None
            self.queue_known = False
            self.backup_current = False
            raise
        self.latest_revision = revision
        self._discover()
        return revision

    async def _photo(self, envelope):
        # Images do not maintain storage and their late response cannot clear queues.
        try:
            value = await self._request(envelope)
            self.latest_revision = max(self.latest_revision, value['revision'])
            if self.cached_state is None or value['revision'] > self.cached_state['revision']:
                self.backup_current = False
            return self._decorate(_captured(value), 'sql')
        except (PrimaryUnavailable, PrimaryProtocolError):
            return self._decorate(_failure('Photo unavailable.'), 'sql', pending=True)
