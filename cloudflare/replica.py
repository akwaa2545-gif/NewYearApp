"""Validated last-good event mirror and durable intents.

Every mutation must run in the caller's database transaction. No network I/O
belongs in this module; a partial replication response never activates a copy.
"""
import base64
import binascii
import hashlib
import json
import math


MAX_QUEUE = 10000
MAX_BLOB = 4 * 1024 * 1024
QUEUE_CHUNK = 512 * 1024
QUEUE_POINTER = b'\x00eventdesk-outbox:v1:'


class ReplicaError(ValueError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def intent_hash(envelope):
    fields = ('operation_id', 'method', 'path', 'body_base64')
    value = {key: envelope[key] for key in fields if key in envelope}
    value['headers'] = sorted((name.lower(), text) for name, text in envelope.get('headers', []))
    return hashlib.sha256(_json(value).encode()).hexdigest()


class ReplicaStore:
    def __init__(self, connection, tables):
        self.db, self.tables = connection, tables
        self.order = self._table_order()

    def _table_order(self):
        pending, ordered = dict(self.tables), []
        while pending:
            ready = [name for name, entry in pending.items() if all(
                foreign[2] == name or foreign[2] not in pending for foreign in entry.get('foreign_keys', []))]
            if not ready:
                raise ReplicaError('Unsupported replica dependency cycle.')
            ordered.extend(sorted(ready))
            pending = {name: entry for name, entry in pending.items() if name not in ready}
        return tuple(ordered)

    def initialize(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS _eventdesk_replica_meta (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
            ready INTEGER NOT NULL, seeded INTEGER NOT NULL, last_sync TEXT)''')
        self.db.execute('''INSERT OR IGNORE INTO _eventdesk_replica_meta
            (singleton,revision,ready,seeded) VALUES (1,0,0,0)''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS _eventdesk_outbox (
            sequence INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE,
            request_hash TEXT NOT NULL, kind TEXT NOT NULL, envelope_json BLOB NOT NULL,
            response_json BLOB NOT NULL, state TEXT NOT NULL DEFAULT 'pending')''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS _eventdesk_outbox_payloads (
            operation_id TEXT NOT NULL REFERENCES _eventdesk_outbox(operation_id) ON DELETE CASCADE,
            field TEXT NOT NULL CHECK(field IN ('envelope','response')), chunk_number INTEGER NOT NULL,
            data BLOB NOT NULL, PRIMARY KEY(operation_id,field,chunk_number))''')

    def _save_payload(self, operation_id, field, value):
        data = _json(value).encode('utf-8')
        maximum = (12 if field == 'envelope' else 24) * 1024 * 1024
        if len(data) > maximum:
            raise ReplicaError('The pending operation is too large.')
        self.db.execute('DELETE FROM _eventdesk_outbox_payloads WHERE operation_id=? AND field=?',
                        (operation_id, field))
        if len(data) <= QUEUE_CHUNK:
            return data
        parts = (len(data) + QUEUE_CHUNK - 1) // QUEUE_CHUNK
        for number in range(parts):
            self.db.execute('''INSERT INTO _eventdesk_outbox_payloads
                (operation_id,field,chunk_number,data) VALUES (?,?,?,?)''',
                (operation_id, field, number, data[number * QUEUE_CHUNK:(number + 1) * QUEUE_CHUNK]))
        metadata = {'length': len(data), 'parts': parts, 'sha256': hashlib.sha256(data).hexdigest()}
        return QUEUE_POINTER + _json(metadata).encode('ascii')

    def _load_payload(self, operation_id, field, data):
        try:
            if isinstance(data, (bytes, bytearray, memoryview)) and bytes(data).startswith(QUEUE_POINTER):
                metadata = json.loads(bytes(data)[len(QUEUE_POINTER):])
                if (not isinstance(metadata, dict) or set(metadata) != {'length', 'parts', 'sha256'}
                        or type(metadata['length']) is not int or not 0 < metadata['length'] <= 24 * 1024 * 1024
                        or type(metadata['parts']) is not int or metadata['parts'] != (metadata['length'] + QUEUE_CHUNK - 1) // QUEUE_CHUNK
                        or not isinstance(metadata['sha256'], str) or len(metadata['sha256']) != 64):
                    raise ReplicaError('Invalid pending operation data.')
                rows = self.db.execute('''SELECT chunk_number,data FROM _eventdesk_outbox_payloads
                    WHERE operation_id=? AND field=? ORDER BY chunk_number''', (operation_id, field)).fetchall()
                if (len(rows) != metadata['parts'] or any(row[0] != index or not isinstance(row[1], bytes)
                        or len(row[1]) > QUEUE_CHUNK for index, row in enumerate(rows))):
                    raise ReplicaError('Incomplete pending operation data.')
                data = b''.join(row[1] for row in rows)
                if len(data) != metadata['length'] or hashlib.sha256(data).hexdigest() != metadata['sha256']:
                    raise ReplicaError('Invalid pending operation data.')
            return json.loads(data)
        except (ValueError, TypeError, KeyError, UnicodeError) as error:
            raise ReplicaError('Invalid pending operation data.') from error

    def state(self):
        row = self.db.execute('SELECT revision,ready,seeded,last_sync FROM _eventdesk_replica_meta WHERE singleton=1').fetchone()
        if row is None:
            raise ReplicaError('Fallback metadata is missing.')
        return {'revision': row[0], 'ready': bool(row[1]), 'seeded': bool(row[2]), 'last_sync': row[3]}

    def _value(self, table, column, value):
        kind = next((item['type'] for item in self.tables[table].get('column_info', [])
                     if item['name'] == column), None)
        if isinstance(value, dict):
            if kind != 'BLOB' or set(value) != {'$bytes'} or not isinstance(value['$bytes'], str):
                raise ReplicaError('Invalid replica binary value.')
            if len(value['$bytes']) > ((MAX_BLOB + 2) // 3) * 4:
                raise ReplicaError('Replica image is too large.')
            try:
                raw = base64.b64decode(value['$bytes'], validate=True)
            except (ValueError, binascii.Error) as error:
                raise ReplicaError('Invalid replica binary value.') from error
            if len(raw) > MAX_BLOB:
                raise ReplicaError('Replica image is too large.')
            return raw
        if value is None or isinstance(value, str):
            if isinstance(value, str) and len(value) > 12 * 1024 * 1024:
                raise ReplicaError('Replica field is too large.')
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if isinstance(value, int) and -(2**63) <= value < 2**63:
                return value
            if isinstance(value, float) and math.isfinite(value):
                return value
        raise ReplicaError('Invalid replica value.')

    def _change(self, change):
        if not isinstance(change, dict) or set(change) != {'table', 'key', 'action', 'row'}:
            raise ReplicaError('Invalid replica change.')
        table = change['table']
        if table not in self.tables:
            raise ReplicaError('Unknown replica table.')
        entry, keys = self.tables[table], change['key']
        if not isinstance(keys, dict) or set(keys) != set(entry['primary_key']):
            raise ReplicaError('Invalid replica key.')
        key = {name: self._value(table, name, value) for name, value in keys.items()}
        if any(value is None or isinstance(value, bytes) for value in key.values()):
            raise ReplicaError('Invalid replica key.')
        action, row = change['action'], change['row']
        if action == 'delete' and row is None:
            return {'table': table, 'key': key, 'action': action, 'row': None}
        if action != 'upsert' or not isinstance(row, dict) or set(row) != set(entry['columns']):
            raise ReplicaError('Invalid replica row.')
        row = {name: self._value(table, name, value) for name, value in row.items()}
        if any(row[name] != value for name, value in key.items()):
            raise ReplicaError('Replica key mismatch.')
        if any(column.get('notnull') and row[column['name']] is None
               for column in entry.get('column_info', [])):
            raise ReplicaError('Required replica field is missing.')
        return {'table': table, 'key': key, 'action': action, 'row': row}

    def _write(self, changes):
        # Workerd explicitly allows this pragma. Enforcement resumes at the
        # caller's transaction commit; never switch it off to hide violations.
        self.db.execute('PRAGMA defer_foreign_keys=ON')
        for table in reversed(self.order):
            for change in changes:
                if change['table'] == table and change['action'] == 'delete':
                    names = self.tables[table]['primary_key']
                    predicate = ' AND '.join('"' + name + '"=?' for name in names)
                    self.db.execute('DELETE FROM "' + table + '" WHERE ' + predicate,
                                    tuple(change['key'][name] for name in names))
        for table in self.order:
            # Stable order also releases old active slots before replacements.
            selected = [change for change in changes if change['table'] == table and change['action'] == 'upsert']
            selected.sort(key=lambda change: tuple(str(change['key'][name]).zfill(24)
                                                  for name in self.tables[table]['primary_key']))
            for change in selected:
                columns, keys = self.tables[table]['columns'], self.tables[table]['primary_key']
                names = ','.join('"' + name + '"' for name in columns)
                updates = ','.join('"' + name + '"=excluded."' + name + '"' for name in columns if name not in keys)
                conflict = ','.join('"' + name + '"' for name in keys)
                suffix = 'DO UPDATE SET ' + updates if updates else 'DO NOTHING'
                sql = ('INSERT INTO "' + table + '" (' + names + ') VALUES (' + ','.join('?' for _ in columns)
                       + ') ON CONFLICT (' + conflict + ') ' + suffix)
                self.db.execute(sql, tuple(change['row'][name] for name in columns))

    @staticmethod
    def _coalesce_sessions(batches):
        # Session activity has no business-table relationships. Keep its final
        # validated state while retaining every other table's revision order.
        last = {_json(change['key']): (batch_number, change_number)
                for batch_number, changes in enumerate(batches)
                for change_number, change in enumerate(changes)
                if change['table'] == 'staff_sessions'}
        return [[change for change_number, change in enumerate(changes)
                 if change['table'] != 'staff_sessions' or
                 last[_json(change['key'])] == (batch_number, change_number)]
                for batch_number, changes in enumerate(batches)]

    def apply_batches(self, batches, head_revision, complete, synced_at=None):
        state = self.state()
        if type(head_revision) is not int or head_revision < state['revision'] or type(complete) is not bool:
            raise ReplicaError('Invalid replica head revision.')
        if not isinstance(batches, list) or len(batches) > 500:
            raise ReplicaError('Invalid replica batch.')
        cursor, validated = state['revision'], []
        for batch in batches:
            if (not isinstance(batch, dict) or set(batch) != {'revision', 'changes'}
                    or type(batch['revision']) is not int or not isinstance(batch['changes'], list)
                    or len(batch['changes']) > 20000):
                raise ReplicaError('Invalid replica batch.')
            changes = [self._change(change) for change in batch['changes']]
            revision = batch['revision']
            if revision <= cursor:
                continue
            if revision != cursor + 1 or revision > head_revision:
                raise ReplicaError('Replica revisions are incomplete.')
            cursor = revision
            validated.append(changes)
        if complete and cursor != head_revision:
            raise ReplicaError('Replica revisions are incomplete.')
        for changes in self._coalesce_sessions(validated):
            self._write(changes)
        ready = state['seeded'] and complete and cursor == head_revision
        self.db.execute('UPDATE _eventdesk_replica_meta SET revision=?,ready=?,last_sync=? WHERE singleton=1',
                        (cursor, int(ready), synced_at))

    def replace_snapshot(self, snapshot, revision, synced_at=None):
        if type(revision) is not int or revision < self.state()['revision']:
            raise ReplicaError('Invalid snapshot revision.')
        if not isinstance(snapshot, dict) or set(snapshot) != set(self.tables):
            raise ReplicaError('Incomplete snapshot.')
        if self.has_pending():
            raise ReplicaError('Reconcile pending operations before replacing the fallback.')
        changes = []
        for table, rows in snapshot.items():
            if not isinstance(rows, list) or len(rows) > 100000:
                raise ReplicaError('Invalid snapshot table.')
            seen = set()
            for row in rows:
                if not isinstance(row, dict):
                    raise ReplicaError('Invalid snapshot row.')
                keys = {name: row.get(name) for name in self.tables[table]['primary_key']}
                change = self._change({'table': table, 'key': keys, 'action': 'upsert', 'row': row})
                identity = _json(keys)
                if identity in seen:
                    raise ReplicaError('Duplicate snapshot identity.')
                seen.add(identity)
                changes.append(change)
        if 'employees' in snapshot and not snapshot['employees']:
            if self.db.execute('SELECT 1 FROM employees LIMIT 1').fetchone():
                raise ReplicaError('Empty SQL data cannot erase the saved employee list.')
        for table in reversed(self.order):
            self.db.execute('DELETE FROM "' + table + '"')
        self._write(changes)
        if self.db.execute('PRAGMA foreign_key_check').fetchone():
            raise ReplicaError('Snapshot relationships are invalid.')
        self.db.execute('''UPDATE _eventdesk_replica_meta SET revision=?,ready=1,seeded=1,last_sync=?
            WHERE singleton=1''', (revision, synced_at))

    def enqueue(self, envelope, kind, response):
        if kind not in ('checkin', 'uncertain') or not isinstance(envelope, dict):
            raise ReplicaError('Invalid pending operation.')
        operation = envelope.get('operation_id')
        if not isinstance(operation, str) or not 1 <= len(operation) <= 100:
            raise ReplicaError('Invalid pending operation.')
        digest = intent_hash(envelope)
        if kind == 'checkin' and (envelope.get('method') != 'POST' or envelope.get('path') != '/api/scan/checkin'):
            raise ReplicaError('Only check-ins can be queued offline.')
        existing = self.db.execute('SELECT request_hash,kind,state FROM _eventdesk_outbox WHERE operation_id=?', (operation,)).fetchone()
        if existing:
            if existing[0] != digest:
                raise ReplicaError('Request ID was already used for another operation.')
            if kind == 'checkin' and existing[1] == 'uncertain' and existing[2] == 'pending':
                response_data = self._save_payload(operation, 'response', response)
                self.db.execute('''UPDATE _eventdesk_outbox SET kind='checkin',response_json=?
                    WHERE operation_id=?''', (response_data, operation))
            return
        count = self.db.execute('SELECT COUNT(*) FROM _eventdesk_outbox').fetchone()[0]
        if count >= MAX_QUEUE:
            raise ReplicaError('Offline queue is full. Reconnect before checking in more employees.')
        self.db.execute('''INSERT INTO _eventdesk_outbox
            (operation_id,request_hash,kind,envelope_json,response_json) VALUES (?,?,?,?,?)''',
                        (operation, digest, kind, b'{}', b'{}'))
        envelope_data = self._save_payload(operation, 'envelope', envelope)
        response_data = self._save_payload(operation, 'response', response)
        self.db.execute('UPDATE _eventdesk_outbox SET envelope_json=?,response_json=? WHERE operation_id=?',
                        (envelope_data, response_data, operation))

    def pending(self, limit=10000):
        if type(limit) is not int or not 1 <= limit <= MAX_QUEUE:
            raise ReplicaError('Invalid pending operation limit.')
        rows = self.db.execute('''SELECT operation_id,kind,envelope_json,response_json,state
            FROM _eventdesk_outbox ORDER BY sequence LIMIT ?''', (limit,)).fetchall()
        return [{'operation_id': row[0], 'kind': row[1], 'envelope': self._load_payload(row[0], 'envelope', row[2]),
                 'response': self._load_payload(row[0], 'response', row[3]), 'state': row[4]} for row in rows]

    def has_pending(self):
        return self.db.execute('SELECT 1 FROM _eventdesk_outbox LIMIT 1').fetchone() is not None

    def pending_metadata(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= MAX_QUEUE:
            raise ReplicaError('Invalid pending operation limit.')
        rows = self.db.execute('''SELECT operation_id,kind,state FROM _eventdesk_outbox
            ORDER BY sequence LIMIT ?''', (limit,)).fetchall()
        return [{'operation_id': row[0], 'kind': row[1], 'state': row[2]} for row in rows]

    def find(self, operation_id):
        row = self.db.execute('''SELECT kind,envelope_json,response_json,state FROM _eventdesk_outbox
            WHERE operation_id=?''', (operation_id,)).fetchone()
        if row is None:
            return None
        return {'operation_id': operation_id, 'kind': row[0], 'envelope': self._load_payload(operation_id, 'envelope', row[1]),
                'response': self._load_payload(operation_id, 'response', row[2]), 'state': row[3]}

    def acknowledge(self, operation_id):
        self.db.execute('DELETE FROM _eventdesk_outbox_payloads WHERE operation_id=?', (operation_id,))
        self.db.execute('DELETE FROM _eventdesk_outbox WHERE operation_id=?', (operation_id,))

    def conflict(self, operation_id):
        self.db.execute("UPDATE _eventdesk_outbox SET state='conflict' WHERE operation_id=?", (operation_id,))

    def invalidate(self):
        self.db.execute('UPDATE _eventdesk_replica_meta SET ready=0 WHERE singleton=1')
