"""SQL-first event routing with a last-good, single-coordinator SQLite fallback.

The injected transport owns HTTP authentication and bounded I/O. ``atomic``
owns each synchronous replica transaction. ``fallback`` must enqueue an accepted
offline check-in in the SAME transaction as its local attendance changes.
"""
import asyncio
import base64
import binascii
from datetime import datetime, timezone
import hashlib
import hmac
import json
import re
import sqlite3

from .replica import ReplicaError, intent_hash


MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 16 * 1024 * 1024
MAX_CHECKIN_BODY = 16 * 1024
READ_POSTS = frozenset(('/api/lookup', '/api/staff-session', '/api/login'))
RESPONSE_HEADERS = frozenset((
    'content-type', 'content-length', 'content-disposition', 'set-cookie',
    'cache-control', 'pragma', 'expires', 'vary', 'allow', 'retry-after',
    'x-content-type-options', 'x-frame-options', 'referrer-policy',
    'strict-transport-security', 'permissions-policy', 'content-security-policy',
))


class PrimaryUnavailable(Exception):
    """Transport failure or unavailable upstream; not an application rejection."""


class PrimaryProtocolError(ValueError):
    """Invalid configuration, authorization or upstream protocol; fail closed."""


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _integer(value):
    return type(value) is int and 0 <= value <= 2**53 - 1


def _failure(message, status=503):
    return {'status': status, 'headers': [('Content-Type', 'application/json; charset=utf-8'),
            ('Cache-Control', 'no-store')], 'body': _json({'error': message}).encode()}


def _captured(value):
    if type(value.get('status')) is not int or not 200 <= value['status'] <= 599:
        raise PrimaryProtocolError('Invalid event response status.')
    encoded, headers = value.get('body_base64'), value.get('headers')
    if not isinstance(encoded, str) or len(encoded) > MAX_RESPONSE_BYTES:
        raise PrimaryProtocolError('Invalid event response size.')
    try:
        body = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise PrimaryProtocolError('Invalid event response encoding.') from None
    if not isinstance(headers, list) or len(headers) > 64:
        raise PrimaryProtocolError('Invalid event response headers.')
    pairs = []
    for pair in headers:
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise PrimaryProtocolError('Invalid event response header.')
        name, text = pair
        if (not isinstance(name, str) or not isinstance(text, str) or name.lower() not in RESPONSE_HEADERS
                or any(char in name + text for char in '\r\n\0') or len(name) + len(text) > 16384):
            raise PrimaryProtocolError('Invalid event response header.')
        if name.lower() == 'content-length':
            if text != str(len(body)):
                raise PrimaryProtocolError('Invalid event response length.')
        else:
            pairs.append((name, text))
    if value['status'] in (204, 304) and body:
        raise PrimaryProtocolError('Invalid empty response.')
    return {'status': value['status'], 'headers': pairs, 'body': body}


class SQLPrimary:
    def __init__(self, transport, atomic, fallback, tables, secret, *, max_replay=20, diagnostic=None):
        if type(max_replay) is not int or not 1 <= max_replay < 10000:
            raise ValueError('Invalid recovery batch size.')
        try:
            self.secret = bytes.fromhex(secret)
        except (TypeError, ValueError):
            raise ValueError('Invalid event signing configuration.') from None
        if len(self.secret) < 32:
            raise ValueError('Invalid event signing configuration.')
        self.transport, self.atomic, self.fallback = transport, atomic, fallback
        self.tables, self.max_replay = tables, max_replay
        self.diagnostic = diagnostic or (lambda _category: None)
        self.backup_available, self.lock = True, asyncio.Lock()
        self.conflict_pending = False
        self.generation = 0

    def _use(self, action):
        try:
            result = self.atomic(action)
            self.backup_available = True
            return result
        except (ReplicaError, sqlite3.IntegrityError) as error:
            raise PrimaryProtocolError('The saved event copy could not be validated.') from error
        except Exception:
            if self.backup_available:
                self.diagnostic('event_backup_unavailable')
            self.backup_available = False
            return None

    def _state(self):
        return self._use(lambda store: store.state())

    def _wire(self, envelope, value):
        if (not isinstance(value, dict) or type(value.get('schema_version')) is not int
                or value['schema_version'] != 1 or value.get('operation_id') != envelope.get('operation_id')
                or not _integer(value.get('after_revision')) or value['after_revision'] != envelope['after_revision']
                or not _integer(value.get('revision')) or value['revision'] < envelope['after_revision']
                or not isinstance(value.get('changes'), list) or type(value.get('replica_complete')) is not bool
                or type(value.get('snapshot_required', False)) is not bool):
            raise PrimaryProtocolError('Invalid event response envelope.')
        _captured(value)
        return value

    async def _request(self, envelope):
        state = self._state()
        message = {**envelope, 'after_revision': state['revision'] if state else 0}
        return self._wire(message, await self.transport('request', message))

    def _proof(self, envelope):
        value = {key: envelope[key] for key in ('operation_id', 'method', 'path', 'body_base64', 'recorded_at')}
        value['cookie'] = next((text for name, text in envelope['headers'] if name.lower() == 'cookie'), '')
        return hmac.new(self.secret, b'eventdesk-offline-checkin-v1\n' + _json(value).encode(), hashlib.sha256).hexdigest()

    def _sync(self, value, operation_id=None):
        def apply(store):
            if operation_id:
                store.acknowledge(operation_id)
            pending = store.has_pending()
            if not pending and not value.get('snapshot_required'):
                state = store.state()
                if state['seeded'] and not state['ready']:
                    return True  # Speculative local identities require a fresh SQL snapshot.
                unchanged = (not value['changes'] and value['replica_complete'] and state['ready']
                             and state['revision'] == value['revision'])
                if not unchanged:
                    store.apply_batches(value['changes'], value['revision'], value['replica_complete'],
                                        datetime.now(timezone.utc).isoformat(timespec='seconds'))
            return not pending
        try:
            return self._use(apply)
        except PrimaryProtocolError as error:
            if not isinstance(error.__cause__, sqlite3.IntegrityError):
                raise
            # A previously acknowledged offline row may have a different SQL ID.
            # The failed delta rolled back; invalidate before replacing it safely.
            if self._use(lambda store: store.has_pending()) is not False:
                raise
            def invalidate(store):
                store.invalidate()
                return True
            return True if self._use(invalidate) is True else None

    def _decorate(self, result, storage, *, pending=False, allow_clear=True):
        headers = [(name, text) for name, text in result['headers'] if not name.lower().startswith('x-eventdesk-')]
        headers.extend([('X-EventDesk-Storage', storage), ('X-EventDesk-Sync', 'pending' if pending else 'synced')])
        if not self.backup_available:
            headers.append(('X-EventDesk-Backup', 'unavailable'))
        elif storage == 'sql' and not pending and allow_clear:
            headers.append(('X-EventDesk-Uncertain', 'clear'))
        return {**result, 'headers': headers}

    async def _recover(self, current_id):
        self.conflict_pending = False
        queued = self._use(lambda store: store.pending_metadata(self.max_replay + 1))
        if queued is None:
            return True
        latest = None
        for item in queued[:self.max_replay]:
            if item['state'] == 'conflict':
                self.conflict_pending = True
                return False
            if item['kind'] == 'uncertain' and item['operation_id'] == current_id:
                continue  # A deliberate retry may use its original idempotency key.
            saved = self._use(lambda store: store.find(item['operation_id']))
            if saved is None:
                if not self.backup_available:
                    return False
                raise PrimaryProtocolError('The pending operation changed during recovery.')
            envelope = saved['envelope']
            if item['kind'] == 'checkin':
                envelope = {**envelope, 'replay': True, 'queue_proof': self._proof(envelope)}
            else:
                envelope = {**envelope, 'receipt_only': True, 'cancel_if_missing': True}
            result = await self._request(envelope)
            if item['kind'] == 'uncertain' and not (result.get('receipt_found') is True or result.get('cancelled') is True):
                raise PrimaryProtocolError('The uncertain request has no verified SQL receipt.')
            if item['kind'] == 'checkin' and result['status'] >= 400:
                self._use(lambda store: store.conflict(item['operation_id']))
                self.conflict_pending = True
                return False
            # Do not apply a SQL delta over remaining unsynchronized local rows.
            def acknowledge(store):
                store.acknowledge(item['operation_id'])
                if item['kind'] == 'checkin':
                    store.invalidate()
            self._use(acknowledge)
            latest = result
        remaining = self._use(lambda store: store.pending_metadata(1))
        if remaining and not (len(remaining) == 1 and remaining[0]['operation_id'] == current_id
                              and remaining[0]['kind'] == 'uncertain'):
            return False
        if latest and not remaining:
            self._sync(latest)
            await self._maintain_backup(latest, True)
        return True

    async def _offline(self, envelope):
        path, method = envelope['path'].split('?', 1)[0], envelope['method']
        state = self._state()
        if not state or not state['seeded'] or not state['ready']:
            return self._decorate(_failure('SQL is unavailable and no verified offline copy is ready.'), 'sqlite', pending=True)
        if self.conflict_pending and method == 'POST' and path not in READ_POSTS:
            return self._decorate(_failure('A pending check-in needs staff review before more changes.'), 'sqlite', pending=True)
        if method == 'POST' and path == '/api/scan/checkin':
            saved = self._use(lambda store: store.find(envelope['operation_id']))
            if saved and saved['kind'] == 'checkin':
                result = _captured(saved['response'])
            else:
                result = await self.fallback(envelope, 'checkin')
            if 200 <= result['status'] < 300:
                try:
                    payload = json.loads(result['body'])
                    if not isinstance(payload, dict):
                        raise ValueError()
                    payload = {**payload, 'pending_sync': True, 'storage': 'sqlite'}
                    headers = [(name, text) for name, text in result['headers'] if name.lower() != 'content-length']
                    result = {**result, 'status': 202, 'headers': headers, 'body': _json(payload).encode()}
                except (ValueError, TypeError, UnicodeError):
                    raise PrimaryProtocolError('Invalid offline check-in result.') from None
            return self._decorate(result, 'sqlite', pending=True)
        if method == 'GET' or method == 'POST' and path in READ_POSTS:
            return self._decorate(await self.fallback(envelope, None), 'sqlite', pending=True)
        return self._decorate(_failure('SQL is unavailable. Draws and changes are paused until it reconnects.'), 'sqlite', pending=True)

    async def handle(self, envelope):
        if (isinstance(envelope, dict) and envelope.get('method') == 'GET'
                and isinstance(envelope.get('path'), str)
                and re.fullmatch(r'/api/employee-photo/[0-9]{7}', envelope['path'])):
            return await self._photo(envelope)
        async with self.lock:
            self.generation += 1
            try:
                return await self._handle(envelope)
            except PrimaryProtocolError as error:
                code = hashlib.sha256(str(error).encode('utf-8')).hexdigest()[:12]
                self.diagnostic('event_primary_protocol_error_' + code)
                result = self._decorate(_failure('The SQL connection needs attention. No offline change was made.'), 'sql', pending=True)
                result['headers'].append(('X-EventDesk-Error-Code', code))
                return result

    async def _handle(self, envelope):
        if not isinstance(envelope, dict) or envelope.get('method') not in ('GET', 'POST'):
            raise PrimaryProtocolError('Invalid event request.')
        envelope = {**envelope, 'headers': [list(pair) for pair in envelope.get('headers', [])]}
        method, path = envelope['method'], envelope['path'].split('?', 1)[0]
        operation_id = envelope.get('operation_id')
        if path == '/api/scan/checkin' and len(envelope.get('body_base64', '')) > 4 * ((MAX_CHECKIN_BODY + 2) // 3):
            return _failure('The check-in request is too large.', 413)
        tracked = method == 'POST' and path not in READ_POSTS
        if tracked:
            saved = self._use(lambda store: store.find(operation_id))
            if saved:
                if intent_hash(saved['envelope']) != intent_hash(envelope):
                    return self._decorate(_failure('This request ID belongs to another action.', 409), 'sqlite', pending=True)
                envelope = saved['envelope']
            elif path == '/api/scan/checkin' and 'recorded_at' not in envelope:
                envelope = {**envelope, 'recorded_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}
        try:
            if not await self._recover(operation_id):
                return await self._offline(envelope)
            if tracked:
                def persist_intent(store):
                    store.enqueue(envelope, 'uncertain', {'status': 503})
                    return True
                if self._use(persist_intent) is not True:
                    self.backup_available = False
                    return self._decorate(_failure('Backup unavailable. Changes are paused.'), 'sql', pending=True)
            result = await self._request(envelope)
        except PrimaryUnavailable:
            return await self._offline(envelope)
        pending_free = self._sync(result, operation_id if tracked else None)
        await self._maintain_backup(result, pending_free)
        return self._decorate(_captured(result), 'sql', pending=pending_free is False)

    async def _maintain_backup(self, value, pending_free):
        """A backup failure cannot turn a known SQL outcome into an unknown one."""
        if pending_free is not True:
            return
        try:
            state = self._state()
            if state and (value.get('snapshot_required') or not state['seeded'] or not state['ready']):
                await self._bootstrap()
        except Exception:
            try:
                self._use(lambda store: store.invalidate())
            except Exception:
                pass  # The original SQL result remains authoritative.
            self.backup_available = False
            self.diagnostic('event_backup_unavailable')

    async def _photo(self, envelope):
        """Photo network I/O must not serialize attendance or prize commands."""
        async with self.lock:
            state = self._state()
            generation = self.generation
            message = {**envelope, 'after_revision': state['revision'] if state else 0}
        try:
            value = self._wire(message, await self.transport('request', message))
        except (PrimaryUnavailable, PrimaryProtocolError):
            return self._decorate(_failure('Photo unavailable.'), 'sql', pending=True, allow_clear=False)
        async with self.lock:
            try:
                state = self._state()
                stale = generation != self.generation or state is not None and value['revision'] < state['revision']
                pending_free = None if stale else self._sync(value)
            except PrimaryProtocolError:
                self.backup_available = False
                stale, pending_free = True, None
            # A late image says nothing about newer pending attendance or draws.
            return self._decorate(_captured(value), 'sql', pending=stale or pending_free is False,
                                  allow_clear=not stale and pending_free is True)

    async def bootstrap(self):
        async with self.lock:
            return await self._bootstrap()

    async def _bootstrap(self):
        if self._use(lambda store: store.has_pending()):
            raise PrimaryProtocolError('Reconcile pending operations before refreshing the fallback.')
        self._use(lambda store: store.invalidate())
        if not self.backup_available:
            raise PrimaryUnavailable('The offline snapshot cannot be stored.')
        discovery = await self.transport('snapshot', {'schema_version': 1})
        if (not isinstance(discovery, dict) or discovery.get('schema_version') != 1
                or not _integer(discovery.get('revision')) or not isinstance(discovery.get('tables'), list)):
            raise PrimaryProtocolError('Invalid snapshot discovery.')
        revision, entries = discovery['revision'], discovery['tables']
        if len(entries) != len(self.tables) or {item.get('name') for item in entries if isinstance(item, dict)} != set(self.tables):
            raise PrimaryProtocolError('Incomplete SQL snapshot.')
        snapshot, size = {}, 0
        for entry in entries:
            table = entry['name']
            if (entry.get('columns') != list(self.tables[table]['columns'])
                    or entry.get('primary_key') != list(self.tables[table]['primary_key'])
                    or not _integer(entry.get('row_count')) or entry['row_count'] > 100000):
                raise PrimaryProtocolError('Invalid snapshot table metadata.')
            rows, offset = [], 0
            while True:
                page = await self.transport('snapshot', {'schema_version': 1, 'table': table,
                    'offset': offset, 'limit': 1000, 'revision': revision})
                if (not isinstance(page, dict) or page.get('schema_version') != 1 or page.get('revision') != revision
                        or page.get('table') != table or not isinstance(page.get('rows'), list) or len(page['rows']) > 1000):
                    raise PrimaryProtocolError('SQL changed during snapshot synchronization.')
                size += len(_json(page).encode())
                if size > MAX_SNAPSHOT_BYTES:
                    raise PrimaryProtocolError('The offline snapshot exceeds the configured size limit.')
                rows.extend(page['rows'])
                if len(rows) > entry['row_count']:
                    raise PrimaryProtocolError('Invalid snapshot row count.')
                next_offset = page.get('next_offset')
                if next_offset is None:
                    break
                if type(next_offset) is not int or next_offset != offset + len(page['rows']) or next_offset <= offset:
                    raise PrimaryProtocolError('Invalid snapshot page cursor.')
                offset = next_offset
            if len(rows) != entry['row_count']:
                raise PrimaryProtocolError('Incomplete snapshot table.')
            snapshot[table] = rows
        activated = self._use(lambda store: store.replace_snapshot(snapshot, revision,
            datetime.now(timezone.utc).isoformat(timespec='seconds')))
        if not self.backup_available:
            raise PrimaryUnavailable('The offline snapshot could not be stored.')
        return revision
