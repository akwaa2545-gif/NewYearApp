"""Validate a complete event export, then explicitly import it into empty SQL tables.

Dry-run is the default and never opens SQL. A commit requires credentials through
stdin, pre-created native tables, and a privileged migration account for identity
preservation. The ordinary runtime account needs no schema-management permission.
"""
from __future__ import annotations

import argparse
import base64
import binascii
from contextlib import closing
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

if __package__ in (None, ''):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eventdesk_storage.schema import TABLES, validate_schema


MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_BLOB_BYTES = 4 * 1024 * 1024
MAX_ROWS = 100000
_BLOB_PREFIX = b'\x00eventdesk-blob:v1:'


class MigrationError(ValueError):
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise MigrationError('Duplicate source field.')
        result[key] = value
    return result


def _wire(value):
    if isinstance(value, bytes):
        return {'$bytes': base64.b64encode(value).decode('ascii')}
    if isinstance(value, dict):
        return {key: _wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_wire(item) for item in value]
    return value


def _json(value):
    return json.dumps(_wire(value), ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _value(column, value):
    if isinstance(value, dict):
        if column['type'] != 'BLOB' or set(value) != {'$bytes'} or not isinstance(value['$bytes'], str):
            raise MigrationError('Invalid binary source field.')
        if len(value['$bytes']) > 4 * ((MAX_BLOB_BYTES + 2) // 3):
            raise MigrationError('Source image is too large.')
        try:
            value = base64.b64decode(value['$bytes'], validate=True)
        except (ValueError, binascii.Error) as error:
            raise MigrationError('Invalid binary source field.') from error
    if value is None:
        if column['notnull'] or column['pk']:
            raise MigrationError('A required source field is empty.')
        return None
    if column['type'] == 'INTEGER':
        if type(value) is not int or not -(2**63) <= value < 2**63:
            raise MigrationError('Invalid source integer.')
    elif column['type'] == 'TEXT':
        try:
            valid_text = isinstance(value, str) and len(value.encode('utf-8')) <= 12 * 1024 * 1024
        except UnicodeError:
            valid_text = False
        if not valid_text:
            raise MigrationError('Invalid source text.')
    elif column['type'] == 'BLOB':
        if not isinstance(value, bytes) or len(value) > MAX_BLOB_BYTES:
            raise MigrationError('Invalid source image.')
        if value.startswith(_BLOB_PREFIX) and re.fullmatch(b'[0-9a-f]{64}', value[len(_BLOB_PREFIX):]):
            raise MigrationError('Source image still contains an unresolved chunk pointer.')
    else:
        raise MigrationError('Unsupported source field type.')
    return value


def validate_snapshot(snapshot):
    if (not isinstance(snapshot, dict) or set(snapshot) != {'schema_version', 'revision', 'tables'}
            or type(snapshot['schema_version']) is not int or snapshot['schema_version'] != 1
            or type(snapshot['revision']) is not int or not 0 <= snapshot['revision'] < 2**63
            or not isinstance(snapshot['tables'], dict) or set(snapshot['tables']) != set(TABLES)):
        raise MigrationError('The source is not a complete version-one event snapshot.')
    tables = {}
    for table, metadata in TABLES.items():
        rows = snapshot['tables'][table]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise MigrationError('Invalid source table size.')
        validated = []
        for row in rows:
            if not isinstance(row, dict) or set(row) != set(metadata['columns']):
                raise MigrationError('Source row columns do not match the approved schema.')
            validated.append({column['name']: _value(column, row[column['name']])
                              for column in metadata['column_info']})
        tables[table] = sorted(validated, key=lambda row: tuple(row[key] for key in metadata['primary_key']))
    if not tables['employees']:
        raise MigrationError('The event export contains no employees; refusing an empty migration.')
    with closing(sqlite3.connect(':memory:', isolation_level=None)) as check:
        check.execute('PRAGMA foreign_keys=ON')
        for metadata in TABLES.values():
            check.execute(metadata['sqlite_sql'])
        for metadata in TABLES.values():
            for index in metadata['indexes']:
                if index['sql']:
                    check.execute(index['sql'])
        check.execute('BEGIN')
        check.execute('PRAGMA defer_foreign_keys=ON')
        try:
            for table, rows in tables.items():
                columns = TABLES[table]['columns']
                sql = ('INSERT INTO "' + table + '" (' + ','.join('"' + name + '"' for name in columns)
                       + ') VALUES (' + ','.join('?' for _ in columns) + ')')
                check.executemany(sql, [tuple(row[name] for name in columns) for row in rows])
            if check.execute('PRAGMA foreign_key_check').fetchone():
                raise MigrationError('Source foreign-key relationships are invalid.')
            check.commit()
        except sqlite3.Error as error:
            raise MigrationError('Source rows violate event integrity constraints.') from error
    return {'schema_version': 1, 'revision': snapshot['revision'], 'tables': tables}


def _read_sqlite(path):
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=5)) as source:
        with closing(sqlite3.connect(':memory:')) as backup:
            deadline = time.monotonic() + 15
            def progress(status, remaining, total):
                if time.monotonic() > deadline:
                    raise MigrationError('Timed out taking a consistent SQLite backup.')
            source.backup(backup, pages=1024, progress=progress, sleep=0.01)
            if backup.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                raise MigrationError('The SQLite source failed its integrity check.')
            names = {row[0] for row in backup.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            missing = set(TABLES) - names
            if missing - {'_eventdesk_projector_images'}:
                raise MigrationError('The SQLite export is missing event tables.')
            tables = {}
            for table, metadata in TABLES.items():
                if table not in names:
                    tables[table] = []
                    continue
                columns = metadata['columns']
                actual = tuple(row[1] for row in backup.execute('PRAGMA table_info("' + table + '")'))
                if actual != columns:
                    raise MigrationError('The SQLite export has an incompatible event schema.')
                values = backup.execute('SELECT * FROM "' + table + '"').fetchmany(MAX_ROWS + 1)
                if len(values) > MAX_ROWS:
                    raise MigrationError('Source table exceeds the migration row limit.')
                records = []
                for values_row in values:
                    row = dict(zip(columns, values_row))
                    for name, value in row.items():
                        suffix = value[len(_BLOB_PREFIX):] if isinstance(value, bytes) and value.startswith(_BLOB_PREFIX) else b''
                        if not re.fullmatch(b'[0-9a-f]{64}', suffix):
                            continue
                        if '_eventdesk_blobs' not in names:
                            raise MigrationError('The SQLite export is missing image chunks.')
                        chunks = backup.execute('SELECT chunk_number,data FROM _eventdesk_blobs WHERE blob_id=? ORDER BY chunk_number',
                                                (suffix.decode('ascii'),)).fetchall()
                        if [number for number, _ in chunks] != list(range(len(chunks))):
                            raise MigrationError('Image chunks are incomplete.')
                        payload = b''.join(bytes(data) for _, data in chunks)
                        if len(payload) > MAX_BLOB_BYTES or hashlib.sha256(payload).hexdigest().encode('ascii') != suffix:
                            raise MigrationError('Image chunks are incomplete or corrupted.')
                        row[name] = payload
                    records.append(row)
                tables[table] = records
    return {'schema_version': 1, 'revision': 0, 'tables': tables}


def load_source(path):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > MAX_SOURCE_BYTES:
        raise MigrationError('Source file is missing or too large.')
    with path.open('rb') as stream:
        magic = stream.read(16)
    if magic == b'SQLite format 3\x00':
        snapshot = _read_sqlite(path)
    else:
        try:
            snapshot = json.loads(path.read_text(encoding='utf-8'), object_pairs_hook=_unique_object)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise MigrationError('The event export is not valid JSON.') from error
    return validate_snapshot(snapshot)


def summarize(snapshot):
    counts, hashes = {}, {}
    for table, rows in snapshot['tables'].items():
        counts[table] = len(rows)
        hashes[table] = hashlib.sha256(_json(rows).encode('utf-8')).hexdigest()
    digest = hashlib.sha256(_json(hashes).encode('utf-8')).hexdigest()
    return {'source_sha256': digest, 'counts': counts, 'table_sha256': hashes}


def _order():
    pending, ordered = dict(TABLES), []
    while pending:
        ready = sorted(name for name, entry in pending.items() if all(
            foreign[2] == name or foreign[2] not in pending for foreign in entry['foreign_keys']))
        if not ready:
            raise MigrationError('Unsupported event table dependency cycle.')
        ordered.extend(ready)
        pending = {name: entry for name, entry in pending.items() if name not in ready}
    return ordered


def migrate(snapshot, connection_string=None, *, schema='eventdesk', commit=False):
    try:
        schema = validate_schema(schema)
    except ValueError as error:
        raise MigrationError('Invalid target event schema.') from error
    snapshot = validate_snapshot(snapshot)
    manifest = summarize(snapshot)
    if not commit:
        return {'mode': 'dry-run', **manifest}
    if not connection_string:
        raise MigrationError('A protected SQL connection is required for commit.')
    import pyodbc
    try:
        raw = pyodbc.connect(connection_string, autocommit=False, timeout=5)
    except pyodbc.Error as error:
        raise MigrationError('Cannot connect to the SQL migration target.') from error
    raw.timeout = 15
    identity_table = None
    try:
        lock = raw.cursor().execute("DECLARE @r INT; EXEC @r=sys.sp_getapplock @Resource=?,"
            "@LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=15000; SELECT @r",
            ('eventdesk:' + schema,)).fetchone()[0]
        if lock < 0:
            raise MigrationError('The event database is busy; migration was not started.')
        receipt = raw.cursor().execute(f'SELECT manifest_json FROM [{schema}].[EventMigrationReceipt] WHERE source_sha256=?',
                                       (manifest['source_sha256'],)).fetchone()
        if receipt:
            if json.loads(receipt[0]) != manifest:
                raise MigrationError('The existing migration receipt does not match the source.')
            raw.rollback()
            return {'mode': 'commit', 'already_imported': True, **manifest}
        for table in TABLES:
            if raw.cursor().execute(f'SELECT TOP 1 1 FROM [{schema}].[{table}]').fetchone():
                raise MigrationError('Target event tables are not empty; refusing to overwrite them.')
        for table in ('EventOperationReceipt', 'EventChangeJournal', 'EventMigrationReceipt'):
            if raw.cursor().execute(f'SELECT TOP 1 1 FROM [{schema}].[{table}]').fetchone():
                raise MigrationError('Target event history is not empty; refusing to overwrite it.')
        deferred = []
        for table in _order():
            metadata, rows = TABLES[table], snapshot['tables'][table]
            columns = metadata['columns']
            self_keys = [foreign[3] for foreign in metadata['foreign_keys'] if foreign[2] == table]
            if metadata['identity'] and rows:
                raw.cursor().execute(f'SET IDENTITY_INSERT [{schema}].[{table}] ON')
                identity_table = table
            sql = (f'INSERT INTO [{schema}].[{table}] (' + ','.join('[' + name + ']' for name in columns)
                   + ') VALUES (' + ','.join('?' for _ in columns) + ')')
            for row in rows:
                values = {**row, **{name: None for name in self_keys}}
                raw.cursor().execute(sql, tuple(values[name] for name in columns))
                for name in self_keys:
                    if row[name] is not None:
                        deferred.append((table, name, row))
            if identity_table:
                raw.cursor().execute(f'SET IDENTITY_INSERT [{schema}].[{table}] OFF')
                identity_table = None
            if metadata['identity'] and rows:
                maximum = max(row[metadata['identity']] for row in rows)
                # Explicit IDENTITY_INSERT values advance SQL Server's seed.
                # Verify that behavior without requiring privileged DBCC access.
                identity = raw.cursor().execute('SELECT CONVERT(BIGINT,IDENT_CURRENT(?))',
                                                 (f'[{schema}].[{table}]',)).fetchone()
                if not identity or identity[0] is None or identity[0] < maximum:
                    raise MigrationError('SQL identity seed could not be verified; migration rolled back.')
        for table, name, row in deferred:
            keys = TABLES[table]['primary_key']
            predicate = ' AND '.join('[' + key + ']=?' for key in keys)
            raw.cursor().execute(f'UPDATE [{schema}].[{table}] SET [{name}]=? WHERE ' + predicate,
                                 (row[name], *(row[key] for key in keys)))
        copied = {}
        for table, metadata in TABLES.items():
            cursor = raw.cursor().execute(f'SELECT * FROM [{schema}].[{table}] ORDER BY '
                + ','.join('[' + key + ']' for key in metadata['primary_key']))
            columns = [column[0] for column in cursor.description]
            copied[table] = [dict(zip(columns, row)) for row in cursor.fetchall()]
        actual = summarize({'tables': copied})
        if actual != manifest:
            raise MigrationError('SQL row counts or hashes do not match the source; migration rolled back.')
        raw.cursor().execute(f'INSERT INTO [{schema}].[EventMigrationReceipt] (source_sha256,manifest_json) VALUES (?,?)',
                             (manifest['source_sha256'], _json(manifest)))
        raw.commit()
        return {'mode': 'commit', 'already_imported': False, **manifest}
    except Exception as error:
        try:
            raw.rollback()
        except pyodbc.Error:
            print('SQL rollback acknowledgment unavailable; reconcile the migration receipt.', file=sys.stderr)
        if isinstance(error, MigrationError):
            raise
        raise MigrationError('SQL migration could not be confirmed. Retry the same source to check its receipt.') from error
    finally:
        try:
            if identity_table:
                try:
                    raw.cursor().execute(f'SET IDENTITY_INSERT [{schema}].[{identity_table}] OFF')
                    raw.rollback()
                except pyodbc.Error:
                    print('SQL identity cleanup acknowledgment unavailable; closing the connection.', file=sys.stderr)
        finally:
            try:
                raw.close()
            except pyodbc.Error:
                print('SQL connection close acknowledgment unavailable.', file=sys.stderr)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--schema', default='eventdesk')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--commit', action='store_true')
    parser.add_argument('--connection-stdin', action='store_true')
    options = parser.parse_args(argv)
    try:
        if options.commit and options.dry_run:
            raise MigrationError('Choose either dry-run or commit.')
        snapshot = load_source(options.source)
        connection = None
        if options.commit:
            if not options.connection_stdin:
                raise MigrationError('Use --connection-stdin for a protected commit connection.')
            raw = sys.stdin.buffer.read(65537)
            if not raw or len(raw) > 65536:
                raise MigrationError('Invalid protected connection input.')
            connection = raw.decode('utf-8').strip()
        result = migrate(snapshot, connection, schema=options.schema, commit=options.commit)
        print(json.dumps(result, sort_keys=True))
        return 0
    except (MigrationError, OSError, UnicodeError) as error:
        print(json.dumps({'error': str(error) if isinstance(error, MigrationError) else 'Cannot read the migration source.'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
