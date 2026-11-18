"""Atomic event state, command receipts, and revisioned replication journal."""
from contextlib import closing, contextmanager
import json
import sqlite3

from eventdesk_storage import TABLES
from eventdesk_storage.schema import validate_schema
from .event_protocol import MAX_RESPONSE_BYTES, ProtocolError, dumps, encode_value


class RevisionConflict(ValueError):
    pass


class _SQLiteConnection:
    """The app can nest handles without committing the owning HTTP transaction."""
    def __init__(self, raw):
        self.raw = raw
        self.rollback_requested = False

    def execute(self, *args):
        if args and args[0].strip().rstrip(';').upper() in ('BEGIN', 'BEGIN IMMEDIATE', 'BEGIN TRANSACTION'):
            return self.raw.execute('SELECT 1 WHERE 0')
        return self.raw.execute(*args)

    def executemany(self, *args):
        return self.raw.executemany(*args)

    def executescript(self, script):
        from cloudflare.sqlite_adapter import _statements
        for statement in _statements(script):
            self.raw.execute(statement)

    def commit(self):
        pass

    def rollback(self):
        self.rollback_requested = True

    def close(self):
        pass


class SQLiteStore:
    """Explicit original-storage mode, also used by offline integration tests."""
    tables = TABLES

    def __init__(self, path):
        self.path = str(path)
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS EventRevision(singleton INTEGER PRIMARY KEY, revision INTEGER NOT NULL);
                INSERT OR IGNORE INTO EventRevision VALUES(1,0);
                CREATE TABLE IF NOT EXISTS EventOperationReceipt(operation_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL,response_json TEXT NOT NULL,revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS EventChangeJournal(revision INTEGER PRIMARY KEY,changes_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS _eventdesk_projector_images(
                    image_id TEXT PRIMARY KEY,content_type TEXT NOT NULL,image BLOB NOT NULL);
            ''')
            db.commit()

    @contextmanager
    def transaction(self):
        raw = sqlite3.connect(self.path, isolation_level=None)
        try:
            raw.row_factory = sqlite3.Row
            raw.execute('PRAGMA foreign_keys=ON')
            raw.execute('BEGIN IMMEDIATE')
            db = _SQLiteConnection(raw)
            tx = EventTransaction(db, '', self.tables, sqlite=True)
            tx.before = tx.all_rows()
            yield tx
            raw.commit()
        except BaseException:
            raw.rollback()
            raise
        finally:
            raw.close()


class SqlServerStore:
    """The normal store uses native SQL tables, never a SQLite image column."""
    tables = TABLES

    def __init__(self, connection_string, *, schema='eventdesk', connection_factory=None):
        from eventdesk_storage import SqlServerConnection
        self.schema = validate_schema(schema)
        self._factory = connection_factory or (lambda: SqlServerConnection(
            connection_string, schema=self.schema, managed=True))

    def initialize(self):
        from eventdesk_storage import initialize_schema
        db = self._factory()
        try:
            initialize_schema(db.raw, self.schema)
            db.raw.commit()
        finally:
            db.raw.close()

    @contextmanager
    def transaction(self):
        db = self._factory()
        try:
            cursor = db.raw.cursor()
            try:
                # pyodbc autocommit=False already opens the owning transaction.
                # An extra BEGIN would nest it and make one commit non-durable.
                cursor.execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;')
                cursor.execute('''DECLARE @result int;
                    EXEC @result=sys.sp_getapplock @Resource=?,@LockMode='Exclusive',
                        @LockOwner='Transaction',@LockTimeout=5000;
                    SELECT @result''', 'eventdesk:' + self.schema)
                if cursor.fetchone()[0] < 0:
                    raise RuntimeError('Event transaction is busy.')
            finally:
                cursor.close()
            yield EventTransaction(db, self.schema, self.tables)
            db.final_commit()
        except BaseException:
            db.final_rollback()
            raise
        finally:
            db.raw.close()


class EventTransaction:
    def __init__(self, db, schema, tables, *, sqlite=False):
        self.db, self.schema, self.tables, self.sqlite = db, schema, tables, sqlite
        self.before = None

    def _name(self, table):
        if table not in self.tables and table not in (
                'EventRevision', 'EventOperationReceipt', 'EventChangeJournal'):
            raise ProtocolError('Unknown event table.')
        return '[' + table + ']' if self.sqlite else '[' + self.schema + '].[' + table + ']'

    def _query(self, sql, values=()):
        with self._rows(sql, values) as rows:
            return list(rows)

    @contextmanager
    def _rows(self, sql, values=()):
        cursor = self.db.raw.cursor()
        try:
            cursor.execute(sql, tuple(values))
            if cursor.description:
                columns = [entry[0] for entry in cursor.description]
                def stream():
                    while (row := cursor.fetchone()) is not None:
                        yield dict(zip(columns, row))
                yield stream()
            else:
                yield iter(())
        finally:
            cursor.close()

    @property
    def revision(self):
        return self._query('SELECT revision FROM ' + self._name('EventRevision') + ' WHERE singleton=1')[0]['revision']

    def receipt(self, operation_id):
        if not operation_id:
            return None
        rows = self._query('SELECT request_hash,response_json,revision FROM '
                           + self._name('EventOperationReceipt') + ' WHERE operation_id=?', (operation_id,))
        if not rows:
            return None
        return {'request_hash': rows[0]['request_hash'], 'response': json.loads(rows[0]['response_json']),
                'revision': rows[0]['revision']}

    def all_rows(self):
        result = {}
        for table, meta in self.tables.items():
            names = ','.join('[' + column + ']' for column in meta['columns'])
            result[table] = self._query('SELECT ' + names + ' FROM ' + self._name(table))
        return result

    def _changes(self):
        if not self.sqlite:
            return self.db.drain_changes()
        after, result = self.all_rows(), []
        for table, meta in self.tables.items():
            keys = meta['primary_key']
            index = lambda rows: {tuple(row[key] for key in keys): row for row in rows}
            before_rows, after_rows = index(self.before[table]), index(after[table])
            for key, row in after_rows.items():
                if before_rows.get(key) != row:
                    result.append({'table': table, 'key': dict(zip(keys, key)), 'action': 'upsert', 'row': row})
            for key in before_rows.keys() - after_rows.keys():
                result.append({'table': table, 'key': dict(zip(keys, key)), 'action': 'delete', 'row': None})
        return result

    def finish(self, operation_id, request_hash, response):
        changes = encode_value(self._changes())
        encoded_changes = dumps(changes)
        if len(encoded_changes) + len(dumps(response)) > MAX_RESPONSE_BYTES - 1024:
            raise ProtocolError('This update is too large to replicate safely. Split it into smaller updates.')
        revision = self.revision
        if changes:
            revision += 1
            self._query('UPDATE ' + self._name('EventRevision') + ' SET revision=? WHERE singleton=1', (revision,))
            self._query('INSERT INTO ' + self._name('EventChangeJournal') + ' (revision,changes_json) VALUES (?,?)',
                        (revision, encoded_changes.decode()))
        if operation_id:
            self._query('INSERT INTO ' + self._name('EventOperationReceipt')
                        + ' (operation_id,request_hash,response_json,revision) VALUES (?,?,?,?)',
                        (operation_id, request_hash, dumps(response).decode(), revision))
        return revision

    def replication(self, after_revision, available_bytes):
        head = self.revision
        if after_revision > head:
            raise RevisionConflict('Replica revision is ahead of SQL.')
        batches, cursor, size = [], after_revision, 0
        found = False
        with self._rows('SELECT revision,changes_json FROM ' + self._name('EventChangeJournal')
                        + ' WHERE revision>? ORDER BY revision', (after_revision,)) as journal:
            for entry in journal:
                found = True
                if entry['revision'] != cursor + 1:
                    return batches, False, True
                batch = {'revision': entry['revision'], 'changes': json.loads(entry['changes_json'])}
                batch_size = len(dumps(batch)) + 1
                if size + batch_size > available_bytes or len(batches) >= 500:
                    break
                batches.append(batch)
                size += batch_size
                cursor = entry['revision']
        return batches, cursor == head, cursor < head and not found

    def snapshot(self, request):
        revision = self.revision
        if request.table is None:
            return {'schema_version': 1, 'revision': revision, 'tables': [
                {'name': table, 'columns': list(meta['columns']), 'primary_key': list(meta['primary_key']),
                 'row_count': self._query('SELECT COUNT(*) AS total FROM ' + self._name(table))[0]['total']}
                for table, meta in self.tables.items()]}
        if request.revision != revision:
            raise RevisionConflict('Snapshot revision changed. Restart discovery.')
        if request.table not in self.tables:
            raise ProtocolError('Unknown snapshot table.')
        meta = self.tables[request.table]
        columns = ','.join('[' + name + ']' for name in meta['columns'])
        order = ','.join('[' + name + ']' for name in meta['primary_key'])
        query = 'SELECT ' + columns + ' FROM ' + self._name(request.table) + ' ORDER BY ' + order
        if self.sqlite:
            query += ' LIMIT ? OFFSET ?'
            values = (request.limit + 1, request.offset)
        else:
            query += ' OFFSET ? ROWS FETCH NEXT ? ROWS ONLY'
            values = (request.offset, request.limit + 1)
        rows, size, more = [], 0, False
        with self._rows(query, values) as source:
            for row in source:
                encoded = encode_value(row)
                row_size = len(dumps(encoded)) + 1
                if len(rows) == request.limit or size + row_size > MAX_RESPONSE_BYTES - 1024:
                    more = True
                    break
                rows.append(encoded)
                size += row_size
        if not rows and more:
            raise ProtocolError('A snapshot record exceeds the response limit.')
        response = {'schema_version': 1, 'revision': revision, 'table': request.table,
                    'rows': rows,
                    'next_offset': request.offset + len(rows) if more else None}
        if len(dumps(response)) > MAX_RESPONSE_BYTES:
            raise ProtocolError('Snapshot page is too large. Request a smaller page.')
        return response
