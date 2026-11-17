"""Private DB-API adapter with real SQL Server transactions and change capture."""
import re
import sqlite3

import pyodbc
import sqlglot
from sqlglot import exp

from .compiler import compile_sql
from .schema import TABLES, validate_schema


class Row:
    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(bytes(value) if isinstance(value, (memoryview, bytearray)) else value for value in values)

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        for index, column in enumerate(self._columns):
            if column.casefold() == str(key).casefold():
                return self._values[index]
        raise IndexError('No item with that key')

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return list(self._columns)


class Cursor:
    def __init__(self, rows=(), columns=(), rowcount=-1, lastrowid=None, row_factory=None):
        self._rows = tuple(Row(columns, row) if row_factory else tuple(row) for row in rows)
        self._position = 0
        self.description = tuple((column, None, None, None, None, None, None) for column in columns) or None
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    def fetchone(self):
        if self._position >= len(self._rows):
            return None
        result = self._rows[self._position]
        self._position += 1
        return result

    def fetchall(self):
        result = list(self._rows[self._position:])
        self._position = len(self._rows)
        return result

    def __iter__(self):
        while (row := self.fetchone()) is not None:
            yield row

    def close(self):
        pass


class SqlServerConnection:
    def __init__(self, connection_string=None, *, schema='eventdesk', managed=False, raw_connection=None):
        self.schema = validate_schema(schema)
        self.managed = bool(managed)
        self.raw = raw_connection if raw_connection is not None else pyodbc.connect(
            connection_string, autocommit=not self.managed, timeout=5)
        self.raw.timeout = 15
        if bool(self.raw.autocommit) == self.managed:
            raise ValueError('The SQL connection transaction mode does not match its owner.')
        self.row_factory = sqlite3.Row
        self.rollback_requested = False
        self._transaction = self.managed
        self._dirty = {}
        self._closed = False

    def _cursor(self, rows=(), columns=(), **kwargs):
        return Cursor(rows, columns, row_factory=self.row_factory, **kwargs)

    def _pragma(self, sql):
        match = re.fullmatch(r'\s*PRAGMA\s+table_info\(\s*([A-Za-z_]\w*)\s*\)\s*;?\s*', sql, re.I)
        if match:
            name = match.group(1)
            if name not in TABLES:
                raise sqlite3.NotSupportedError('Unknown event table.')
            columns = ('cid', 'name', 'type', 'notnull', 'dflt_value', 'pk')
            rows = [(index, c['name'], c['type'], c['notnull'], c['default'], c['pk'])
                    for index, c in enumerate(TABLES[name]['column_info'])]
            return self._cursor(rows, columns)
        if re.fullmatch(r'\s*PRAGMA\s+(journal_mode\s*=\s*WAL|foreign_keys\s*=\s*ON|busy_timeout\s*=\s*\d+)\s*;?\s*', sql, re.I):
            return self._cursor()
        raise sqlite3.NotSupportedError('Unsupported SQLite pragma.')

    def _known_ddl(self, sql):
        tree = sqlglot.parse_one(sql, read='sqlite')
        if not isinstance(tree, exp.Create):
            raise sqlite3.NotSupportedError('Only fixed schema creation is supported.')
        if isinstance(tree.this, exp.Schema) and isinstance(tree.this.this, exp.Table):
            table = tree.this.this.name
            columns = tuple(column.name for column in tree.this.expressions if isinstance(column, exp.ColumnDef))
            if table in TABLES and columns == TABLES[table]['columns']:
                expected = sqlglot.parse_one(TABLES[table]['sqlite_sql'], read='sqlite')
                actual_fields = tuple(field.sql('sqlite', normalize=True) for field in tree.this.expressions)
                expected_fields = tuple(field.sql('sqlite', normalize=True) for field in expected.this.expressions)
                if actual_fields == expected_fields:
                    return self._cursor()
        elif isinstance(tree.this, exp.Index):
            table = tree.this.args.get('table')
            if isinstance(table, exp.Table) and table.name in TABLES:
                index_names = {index['name'] for index in TABLES[table.name]['indexes']}
                if tree.this.name in index_names:
                    source = next(index['sql'] for index in TABLES[table.name]['indexes']
                                  if index['name'] == tree.this.name)
                    expected = sqlglot.parse_one(source, read='sqlite')
                    if tree.this.sql('sqlite', normalize=True) == expected.this.sql('sqlite', normalize=True):
                        return self._cursor()
        raise sqlite3.NotSupportedError('Schema does not match the installed native migration.')

    def execute(self, statement, parameters=()):
        if self._closed:
            raise sqlite3.ProgrammingError('Connection is closed.')
        stripped = statement.strip().rstrip(';').strip()
        if stripped.upper().startswith('PRAGMA '):
            return self._pragma(stripped)
        if stripped.upper() in ('BEGIN', 'BEGIN IMMEDIATE', 'BEGIN TRANSACTION'):
            if not self.managed:
                if self._transaction:
                    raise sqlite3.OperationalError('Transaction already active.')
                self.raw.cursor().execute('SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; BEGIN TRANSACTION')
                lock = self.raw.cursor().execute(
                    "DECLARE @result INT; EXEC @result=sys.sp_getapplock @Resource=?,"
                    " @LockMode='Exclusive',@LockOwner='Transaction',@LockTimeout=15000; SELECT @result",
                    ('eventdesk:' + self.schema,)).fetchone()[0]
                if lock < 0:
                    self.raw.cursor().execute('ROLLBACK TRANSACTION')
                    raise sqlite3.OperationalError('Timed out waiting for the event transaction lock.')
                self._transaction = True
            return self._cursor()
        if stripped.upper() in ('COMMIT', 'END'):
            self.commit()
            return self._cursor()
        if stripped.upper() == 'ROLLBACK':
            self.rollback()
            return self._cursor()
        if stripped.upper().startswith('CREATE '):
            return self._known_ddl(stripped)
        compiled = compile_sql(statement, tuple(parameters), self.schema)
        raw = self.raw.cursor()
        try:
            raw.execute(compiled.sql, compiled.parameters)
            while raw.description is None and raw.nextset():
                pass
            columns = tuple(item[0] for item in raw.description) if raw.description else ()
            values = raw.fetchall() if raw.description else []
        except pyodbc.IntegrityError as error:
            # With XACT_ABORT off, ignored duplicate/CHECK/NOTNULL statements do
            # not poison the enclosing transaction. Foreign keys still fail.
            message = str(error)
            if compiled.ignore and (any(f'({code})' in message for code in (2601, 2627, 515))
                                    or ('(547)' in message and 'CHECK constraint' in message)):
                return self._cursor(rowcount=0)
            raise sqlite3.IntegrityError('SQL Server constraint rejected the change.') from error
        except pyodbc.Error as error:
            raise sqlite3.OperationalError('SQL Server query failed.') from error
        finally:
            raw.close()
        if compiled.table is None:
            return self._cursor(values, columns)
        metadata = TABLES[compiled.table]
        for row in values:
            key = tuple(row[index] for index in range(len(metadata['primary_key'])))
            self._dirty[(compiled.table, key)] = True
        lastrowid = values[-1][0] if values and compiled.kind == 'insert' and metadata['identity'] else None
        return self._cursor(rowcount=len(values), lastrowid=lastrowid)

    def executemany(self, statement, values):
        count = 0
        last = None
        for parameters in values:
            result = self.execute(statement, parameters)
            count += max(0, result.rowcount)
            last = result.lastrowid
        return self._cursor(rowcount=count, lastrowid=last)

    def executescript(self, script):
        for tree in sqlglot.parse(script, read='sqlite'):
            if tree is not None:
                self.execute(tree.sql('sqlite'))
        return self._cursor()

    def drain_changes(self):
        changes = []
        for table, values in sorted(self._dirty):
            keys = TABLES[table]['primary_key']
            where = ' AND '.join('[' + column + ']=?' for column in keys)
            cursor = self.raw.cursor()
            cursor.execute(f'SELECT * FROM [{self.schema}].[{table}] WHERE ' + where, values)
            columns = [item[0] for item in cursor.description]
            found = cursor.fetchone()
            cursor.close()
            row = dict(Row(columns, found)) if found is not None else None
            changes.append({'table': table, 'key': dict(zip(keys, values)),
                            'action': 'upsert' if row is not None else 'delete', 'row': row})
        self._dirty = {}
        return changes

    def commit(self):
        if not self.managed:
            self.final_commit()

    def rollback(self):
        self.rollback_requested = True
        if not self.managed:
            self.final_rollback()

    def final_commit(self):
        if self.rollback_requested:
            raise sqlite3.OperationalError('A rolled-back request cannot commit.')
        if not self.managed and self._transaction:
            self.raw.cursor().execute('COMMIT TRANSACTION')
        else:
            self.raw.commit()
        self._transaction = self.managed

    def final_rollback(self):
        if not self.managed and self._transaction:
            self.raw.cursor().execute('ROLLBACK TRANSACTION')
        else:
            self.raw.rollback()
        self._transaction = self.managed
        self.rollback_requested = False
        self._dirty = {}

    def close(self):
        if self.managed:
            return
        if not self._closed and not self.managed:
            if self._transaction:
                self.final_rollback()
            self.raw.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, kind, error, traceback):
        if error:
            self.rollback()
        elif not self.managed:
            self.commit()
        self.close()
