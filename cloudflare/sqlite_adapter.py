"""SQLite DB-API subset for a synchronous Durable Object SQL binding.

The request owner MUST wrap all calls in storage.transactionSync() and abort
that transaction when TransactionState.rollback_requested is true. Transaction
commands here are markers, not database transactions. Never use this adapter
without that outer transaction: doing so would discard rollback semantics.
"""
from dataclasses import dataclass
from bisect import bisect_left
import hashlib
import json
import re
import sqlite3

try:
    from pyodide.ffi import jsnull as _JS_NULL
except ImportError:
    _JS_NULL = object()  # Native Python has no JavaScript-null singleton.

_BLOB_PREFIX = b'\x00eventdesk-blob:v1:'
_BLOB_THRESHOLD = 1024 * 1024
_BLOB_CHUNK_BYTES = 512 * 1024
_SQL_NONCODE = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|`(?:``|[^`])*`|\[[^\]]*\]|--[^\r\n]*|/\*[\s\S]*?\*/")
_SIMPLE_IN = re.compile(r'\bIN\s*\(\s*\?(?:\s*,\s*\?)*\s*\)', re.I)
_SIMPLE_LIKE = re.compile(r'(?P<column>\b[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?)\s+(?P<not>NOT\s+)?LIKE\s*\?', re.I)


@dataclass
class TransactionState:
    rollback_requested: bool = False


class SQLiteRow:
    """Retain sqlite3.Row's value iteration and named/positional lookup."""

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        if isinstance(key, str):
            for index, name in enumerate(self._columns):
                if name.casefold() == key.casefold():
                    return self._values[index]
        raise IndexError('No item with that key')

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._columns)


def _statements(script):
    """Use SQLite's parser to preserve quoted semicolons and trigger bodies."""
    pending = ''
    for character in script:
        pending += character
        if character == ';' and sqlite3.complete_statement(pending):
            yield pending
            pending = ''
    if pending.strip():
        yield pending


def _plain(value):
    if value is _JS_NULL:
        return None
    converter = getattr(value, 'to_py', None)
    converted = converter() if callable(converter) else value
    return None if converted is _JS_NULL else converted


def _leading_sql(statement):
    return re.sub(r'\A(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*', '', statement, flags=re.S)


def _sql_mask(statement):
    """Hide quoted strings/identifiers and comments without moving offsets."""
    return _SQL_NONCODE.sub(lambda match: ' ' * len(match.group()), statement)


def _compact_in_bindings(statement, parameters):
    if len(parameters) <= 100:
        return statement, parameters
    mask = _sql_mask(statement)
    positions = [match.start() for match in re.finditer(r'\?', mask)]
    if len(positions) != len(parameters) or re.search(r'\?\d', mask):
        raise sqlite3.NotSupportedError('Queries above 100 parameters require simple positional IN lists.')
    fragments, compacted = [], []
    sql_offset = parameter_offset = 0
    for match in _SIMPLE_IN.finditer(mask):
        first = bisect_left(positions, match.start())
        last = bisect_left(positions, match.end())
        values = parameters[first:last]
        if len(values) < 2 or any(value is not None and not isinstance(value, (str, int, float)) for value in values):
            continue
        try:
            encoded = json.dumps(values, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        except (ValueError, TypeError) as exc:
            raise sqlite3.NotSupportedError('Large IN lists require finite JSON scalar values.') from exc
        # Unary + removes json_each.value's affinity, matching individual IN values.
        fragments.extend((statement[sql_offset:match.start()], 'IN (SELECT +value FROM json_each(?))'))
        compacted.extend(parameters[parameter_offset:first])
        compacted.append(encoded)
        sql_offset, parameter_offset = match.end(), last
    fragments.append(statement[sql_offset:])
    compacted.extend(parameters[parameter_offset:])
    if len(compacted) > 100:
        raise sqlite3.NotSupportedError('This query exceeds the hosted limit of 100 bound parameters.')
    return ''.join(fragments), tuple(compacted)


def _rewrite_long_like(statement, parameters):
    if not any(isinstance(value, str) and len(value.encode('utf-8')) > 50 for value in parameters):
        return statement, parameters
    mask = _sql_mask(statement)
    positions = [match.start() for match in re.finditer(r'\?', mask)]
    values, fragments, offset = list(parameters), [], 0
    for match in _SIMPLE_LIKE.finditer(mask):
        index = bisect_left(positions, match.end() - 1)
        if index >= len(values):
            continue  # Let SQLite report an ordinary binding mismatch.
        pattern = values[index]
        if not isinstance(pattern, str) or len(pattern.encode('utf-8')) <= 50:
            continue
        prefix = mask[:match.start()].rstrip()
        if prefix and not re.search(r'(?:\bWHERE|\bAND|\bOR|\bON|\bWHEN|\bTHEN|\bELSE|\bSELECT|[(,])$', prefix, re.I):
            raise ValueError('This search expression is too long. Use a shorter search.')
        literal = pattern[1:-1]
        if (not pattern.startswith('%') or not pattern.endswith('%') or '%' in literal or '_' in literal
                or re.match(r'\s+ESCAPE\b', mask[match.end():], re.I)):
            raise ValueError('Search text with wildcards is too long. Use a shorter search.')
        values[index] = literal
        operator = '= 0' if match.group('not') else '> 0'
        replacement = f"instr(lower({match.group('column')}), lower(?)) {operator}"
        fragments.extend((statement[offset:match.start()], replacement))
        offset = match.end()
    fragments.append(statement[offset:])
    return ''.join(fragments), tuple(values)


class SQLiteCursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rowcount = -1
        self.lastrowid = None
        self.arraysize = 1
        self._rows = []
        self._position = 0

    def execute(self, statement, parameters=()):
        self.connection._check_open()
        self.description = None
        self.rowcount = -1
        self._rows = []
        self._position = 0
        command = _leading_sql(statement).strip().rstrip(';').strip()
        if self.connection._transaction_command(command):
            return self
        if re.fullmatch(r'PRAGMA\s+(busy_timeout|journal_mode)\s*=.*', command, re.I):
            # Durable Objects own locking and journaling; these are host settings.
            return self
        columns, rows = self.connection._query(statement, parameters)
        if columns:
            self.description = [(name, None, None, None, None, None, None) for name in columns]
            self._rows = [tuple(self.connection._decode_value(value) for value in row) for row in rows]
        if re.match(r'(INSERT|UPDATE|DELETE|REPLACE)\b', command, re.I):
            _, metadata = self.connection._query(
                'SELECT changes() AS changed, last_insert_rowid() AS inserted_id', ())
            self.rowcount, self.lastrowid = metadata[0]
        return self

    def executemany(self, statement, parameter_rows):
        count = 0
        for parameters in parameter_rows:
            self.execute(statement, parameters)
            count += max(0, self.rowcount)
        self.rowcount = count
        return self

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        values = self._rows[self._position]
        self._position += 1
        factory = self.connection.row_factory
        if factory is sqlite3.Row or factory is SQLiteRow:
            return SQLiteRow([column[0] for column in self.description], values)
        return factory(self, values) if factory is not None else values

    def fetchmany(self, size=None):
        size = self.arraysize if size is None else size
        if size < 0:
            raise ValueError('Array size must be non-negative.')
        result = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            result.append(row)
        return result

    def fetchall(self):
        return list(self)

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class SQLiteConnection:
    """DB-API facade; all connections for a request share TransactionState."""

    def __init__(self, sql, transaction_state=None, bind_value=None, from_js=None):
        self._sql = sql
        self.transaction_state = transaction_state if transaction_state is not None else TransactionState()
        self._bind_value = bind_value or (lambda value: value)
        self._from_js = from_js or _plain
        self.row_factory = None
        self.in_transaction = False
        self._closed = False

    @property
    def rollback_requested(self):
        return self.transaction_state.rollback_requested

    def _check_open(self):
        if self._closed:
            raise sqlite3.ProgrammingError('Cannot operate on a closed database.')

    def _transaction_command(self, command):
        if re.fullmatch(r'BEGIN(?:\s+(?:DEFERRED|IMMEDIATE|EXCLUSIVE))?(?:\s+TRANSACTION)?', command, re.I):
            if self.in_transaction:
                raise sqlite3.OperationalError('Cannot start a transaction within a transaction.')
            self.in_transaction = True
            return True
        if re.fullmatch(r'(COMMIT|END)(?:\s+TRANSACTION)?', command, re.I):
            self.commit()
            return True
        if re.fullmatch(r'ROLLBACK(?:\s+TRANSACTION)?', command, re.I):
            self.rollback()
            return True
        if re.match(r'(SAVEPOINT|RELEASE|ROLLBACK\s+TO)\b', command, re.I):
            raise sqlite3.NotSupportedError('Nested savepoints are not supported by this adapter.')
        return False

    def _query(self, statement, parameters):
        if isinstance(parameters, dict):
            raise sqlite3.NotSupportedError('Use positional SQL parameters with this adapter.')
        statement, parameters = _compact_in_bindings(statement, tuple(parameters))
        statement, parameters = _rewrite_long_like(statement, parameters)
        try:
            values = [self._store_value(value) for value in parameters]
            result = self._sql.exec(statement, *(self._bind_value(value) for value in values))
            columns = list(_plain(result.columnNames))
            rows = _plain(result.raw().toArray())
            return columns, rows
        except sqlite3.Error:
            raise
        except Exception as exc:
            message = str(exc)
            error_type = sqlite3.IntegrityError if re.search(
                r'SQLITE_CONSTRAINT|constraint failed|constraint violation', message, re.I
            ) else sqlite3.OperationalError
            raise error_type(message) from exc

    def _store_value(self, value):
        if not isinstance(value, (bytes, bytearray, memoryview)):
            return value
        value = bytes(value)
        if len(value) <= _BLOB_THRESHOLD and not value.startswith(_BLOB_PREFIX):
            return value
        blob_id = hashlib.sha256(value).hexdigest()
        self._sql.exec('''CREATE TABLE IF NOT EXISTS _eventdesk_blobs (
            blob_id TEXT NOT NULL, chunk_number INTEGER NOT NULL, data BLOB NOT NULL,
            PRIMARY KEY (blob_id, chunk_number)) WITHOUT ROWID''')
        for index, offset in enumerate(range(0, len(value), _BLOB_CHUNK_BYTES)):
            self._sql.exec('''INSERT OR IGNORE INTO _eventdesk_blobs
                (blob_id, chunk_number, data) VALUES (?, ?, ?)''',
                blob_id, index, self._bind_value(value[offset:offset + _BLOB_CHUNK_BYTES]))
        return _BLOB_PREFIX + blob_id.encode('ascii')

    def _decode_value(self, value):
        value = _plain(self._from_js(value))
        if not isinstance(value, (bytes, bytearray, memoryview)):
            return value
        value = bytes(value)
        suffix = value[len(_BLOB_PREFIX):] if value.startswith(_BLOB_PREFIX) else b''
        if not re.fullmatch(b'[0-9a-f]{64}', suffix):
            return value
        blob_id = suffix.decode('ascii')
        result = self._sql.exec('''SELECT data FROM _eventdesk_blobs
            WHERE blob_id=? ORDER BY chunk_number''', blob_id)
        chunks = _plain(result.raw().toArray())
        content = b''.join(bytes(self._from_js(row[0])) for row in chunks)
        if hashlib.sha256(content).hexdigest() != blob_id:
            raise sqlite3.DatabaseError('Stored image data is incomplete or corrupted.')
        return content

    def cursor(self):
        self._check_open()
        return SQLiteCursor(self)

    def execute(self, statement, parameters=()):
        return self.cursor().execute(statement, parameters)

    def executemany(self, statement, parameter_rows):
        return self.cursor().executemany(statement, parameter_rows)

    def executescript(self, script):
        cursor = self.cursor()
        for statement in _statements(script):
            cursor.execute(statement)
        return cursor

    def commit(self):
        self._check_open()
        self.in_transaction = False

    def rollback(self):
        self._check_open()
        self.transaction_state.rollback_requested = True
        self.in_transaction = False

    def close(self):
        if self.in_transaction:
            self.transaction_state.rollback_requested = True
        self.in_transaction = False
        self._closed = True

    def __enter__(self):
        self._check_open()
        return self

    def __exit__(self, exception_type, _exception, _traceback):
        self.rollback() if exception_type else self.commit()
