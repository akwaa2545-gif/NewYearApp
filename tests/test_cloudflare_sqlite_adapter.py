"""Exercise the Workers SQL adapter against real SQLite query semantics."""
from pathlib import Path
from contextlib import contextmanager
import io
import json
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cloudflare.sqlite_adapter import SQLiteConnection, TransactionState
import cloudflare.sqlite_adapter as adapter


class RawRows:
    def __init__(self, rows):
        self.rows = rows

    def toArray(self):
        return self.rows


class SQLResult:
    def __init__(self, cursor):
        self.columnNames = [column[0] for column in cursor.description or ()]
        self.rows = cursor.fetchall()

    def raw(self):
        return RawRows(self.rows)


class FakeSQL:
    def __init__(self):
        self.database = sqlite3.connect(':memory:', isolation_level=None)
        self.statements = []
        self.parameter_counts = []

    def exec(self, statement, *parameters):
        self.statements.append(statement)
        self.parameter_counts.append(len(parameters))
        if len(parameters) > 100:
            raise sqlite3.OperationalError('Durable Objects allow at most 100 bound parameters.')
        if any(isinstance(value, bytes) and len(value) > 2 * 1024 * 1024 for value in parameters):
            raise sqlite3.DataError('A Durable Object row cannot exceed 2 MB.')
        if statement.strip().upper().startswith(('BEGIN', 'COMMIT', 'ROLLBACK')):
            raise AssertionError('The adapter must defer transactions to its outer request.')
        return SQLResult(self.database.execute(statement, parameters))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.sql = FakeSQL()
        self.addCleanup(self.sql.database.close)
        self.connection = SQLiteConnection(self.sql)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT UNIQUE, picture BLOB)')

    def test_rows_support_mapping_and_positional_access(self):
        self.connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))
        row = self.connection.execute('SELECT id, name FROM people').fetchone()
        self.assertEqual(dict(row), {'id': 1, 'name': 'Ada'})
        self.assertEqual(tuple(row), (1, 'Ada'))
        self.assertEqual(row[0], 1)
        self.assertEqual(row[-1], 'Ada')
        self.assertEqual(row['NAME'], 'Ada')
        self.assertEqual(row[:1], (1,))

    def test_fetch_and_iteration_advance_one_cursor(self):
        self.connection.executemany('INSERT INTO people (name) VALUES (?)', [('Ada',), ('Grace',), ('Linus',)])
        cursor = self.connection.execute('SELECT name FROM people ORDER BY id')
        self.assertEqual(cursor.fetchone()[0], 'Ada')
        self.assertEqual([row[0] for row in cursor.fetchmany(1)], ['Grace'])
        self.assertEqual([row[0] for row in cursor], ['Linus'])
        self.assertIsNone(cursor.fetchone())
        self.assertEqual(cursor.fetchall(), [])

    def test_default_rows_are_tuples(self):
        connection = SQLiteConnection(self.sql)
        self.assertEqual(connection.execute('SELECT 12 AS number').fetchone(), (12,))

    def test_duplicate_columns_keep_positional_values(self):
        row = self.connection.execute('SELECT 1 AS value, 2 AS value').fetchone()
        self.assertEqual(tuple(row), (1, 2))
        self.assertEqual(row['value'], 1)
        self.assertEqual(row.keys(), ['value', 'value'])

    def test_insert_and_ignore_report_changes_and_lastrowid(self):
        inserted = self.connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))
        ignored = self.connection.execute('INSERT OR IGNORE INTO people (name) VALUES (?)', ('Ada',))
        self.assertEqual(inserted.rowcount, 1)
        self.assertEqual(inserted.lastrowid, 1)
        self.assertEqual(ignored.rowcount, 0)
        self.assertEqual(ignored.lastrowid, 1)
        self.assertEqual(self.connection.execute('SELECT * FROM people').rowcount, -1)

    def test_parameter_binding_does_not_interpret_sql(self):
        name = "Robert'); DROP TABLE people; --"
        self.connection.execute('INSERT INTO people (name) VALUES (?)', (name,))
        self.assertEqual(self.connection.execute('SELECT name FROM people').fetchone()[0], name)

    def test_binary_parameters_and_results_are_preserved(self):
        data = b'\x00\xffimage\x00'
        self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Ada', data))
        self.assertEqual(self.connection.execute('SELECT picture FROM people').fetchone()[0], data)

    def test_parameter_conversion_is_injectable_for_js_blobs(self):
        seen = []
        def convert(value):
            seen.append(value)
            return value
        connection = SQLiteConnection(self.sql, bind_value=convert)
        connection.execute('SELECT ?, ?', (b'image', 4))
        self.assertEqual(seen, [b'image', 4])

    def test_large_blob_uses_bounded_chunks_and_round_trips(self):
        data = b'\x00image\xff' * (600 * 1024)
        self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Ada', data))
        raw = self.sql.database.execute('SELECT picture FROM people').fetchone()[0]
        self.assertLess(len(raw), 1024)
        self.assertEqual(self.connection.execute('SELECT picture FROM people').fetchone()[0], data)
        chunks = self.sql.database.execute('SELECT data FROM _eventdesk_blobs').fetchall()
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(row[0]) <= 512 * 1024 for row in chunks))

    def test_chunk_storage_preserves_insert_identity_and_deduplicates(self):
        data = b'image' * (300 * 1024)
        first = self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Ada', data))
        first_chunk_count = self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_blobs').fetchone()[0]
        second = self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Grace', data))
        self.assertEqual((first.lastrowid, second.lastrowid), (1, 2))
        self.assertEqual((first.rowcount, second.rowcount), (1, 1))
        self.assertEqual(self.sql.database.execute('SELECT COUNT(*) FROM _eventdesk_blobs').fetchone()[0], first_chunk_count)

    def test_user_bytes_resembling_an_internal_marker_round_trip(self):
        data = b'\x00eventdesk-blob:v1:' + b'a' * 64
        self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Ada', data))
        self.assertEqual(self.connection.execute('SELECT picture FROM people').fetchone()[0], data)

    def test_missing_blob_chunk_is_reported_instead_of_returning_corrupt_data(self):
        data = b'image' * (300 * 1024)
        self.connection.execute('INSERT INTO people (name, picture) VALUES (?, ?)', ('Ada', data))
        self.sql.database.execute('DELETE FROM _eventdesk_blobs WHERE chunk_number=0')
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute('SELECT picture FROM people').fetchone()

    def test_executemany_reports_total_affected_rows(self):
        cursor = self.connection.executemany('INSERT INTO people (name) VALUES (?)', [('Ada',), ('Grace',)])
        self.assertEqual(cursor.rowcount, 2)
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM people').fetchone()[0], 2)

    def test_script_splitting_preserves_quotes_comments_and_triggers(self):
        self.connection.executescript('''
            -- a comment containing ;
            INSERT INTO people (name) VALUES ('semi;colon');
            CREATE TRIGGER copy_person AFTER INSERT ON people
            WHEN NEW.name = 'Ada'
            BEGIN
                INSERT INTO people (name) VALUES ('Ada copy');
            END;
            INSERT INTO people (name) VALUES ('Ada');
        ''')
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM people').fetchone()[0], 3)

    def test_begin_commit_and_rollback_are_deferred_and_shared(self):
        state = TransactionState()
        first = SQLiteConnection(self.sql, transaction_state=state)
        second = SQLiteConnection(self.sql, transaction_state=state)
        first.execute('BEGIN IMMEDIATE')
        self.assertTrue(first.in_transaction)
        first.commit()
        self.assertFalse(first.in_transaction)
        second.execute('BEGIN')
        second.execute('ROLLBACK')
        self.assertTrue(first.rollback_requested)
        self.assertTrue(second.rollback_requested)
        first.commit()
        self.assertTrue(first.rollback_requested, 'Commit cannot erase a request rollback.')

    def test_request_owner_can_rollback_all_sql_changes(self):
        state = TransactionState()
        connection = SQLiteConnection(self.sql, transaction_state=state)
        self.sql.database.execute('BEGIN')
        connection.execute('BEGIN IMMEDIATE')
        connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))
        connection.rollback()
        if state.rollback_requested:
            self.sql.database.rollback()
        self.assertEqual(self.sql.database.execute('SELECT COUNT(*) FROM people').fetchone()[0], 0)

    def test_integrity_errors_remain_sqlite_integrity_errors(self):
        self.connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))

    def test_close_blocks_queries_but_does_not_close_shared_sql(self):
        self.connection.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            self.connection.execute('SELECT 1')
        self.assertEqual(SQLiteConnection(self.sql).execute('SELECT 1').fetchone(), (1,))

    def test_large_in_list_matches_native_sqlite_with_at_most_100_bindings(self):
        self.connection.executemany('INSERT INTO people (name) VALUES (?)', [(f'person-{i}',) for i in range(500)])
        parameters = list(range(1, 501))
        statement = 'SELECT id FROM people WHERE id IN (' + ','.join('?' for _ in parameters) + ') ORDER BY id'
        expected = self.sql.database.execute(statement, parameters).fetchall()
        actual = [tuple(row) for row in self.connection.execute(statement, parameters)]
        self.assertEqual(actual, expected)
        self.assertIn('json_each', self.sql.statements[-1])
        self.assertLessEqual(max(self.sql.parameter_counts), 100)

    def test_large_in_list_preserves_outside_parameters_quotes_and_comments(self):
        self.connection.executemany('INSERT INTO people (name) VALUES (?)', [('Ada',), ('Grace',)])
        placeholders = ','.join('?' for _ in range(500))
        statement = "SELECT id, '?' AS literal FROM people WHERE name<>? AND id IN (" + placeholders + ") AND id<? /* ? */ -- ?\n"
        parameters = ['Grace', *range(1, 501), 3]
        expected = self.sql.database.execute(statement, parameters).fetchall()
        actual = [tuple(row) for row in self.connection.execute(statement, parameters)]
        self.assertEqual(actual, expected)
        self.assertIn("'?' AS literal", self.sql.statements[-1])
        self.assertEqual(self.sql.parameter_counts[-1], 3)

    def test_multiple_in_lists_keep_parameter_order(self):
        self.connection.executemany('INSERT INTO people (name) VALUES (?)', [('Ada',), ('Grace',)])
        placeholders = ','.join('?' for _ in range(150))
        statement = 'SELECT id FROM people WHERE id IN (' + placeholders + ') AND id IN (' + placeholders + ')'
        parameters = [*range(1, 151), *range(2, 152)]
        self.assertEqual([tuple(row) for row in self.connection.execute(statement, parameters)], [(2,)])
        self.assertEqual(self.sql.parameter_counts[-1], 2)

    def test_unsupported_excess_parameters_fail_clearly_before_sql_execution(self):
        previous = len(self.sql.statements)
        with self.assertRaisesRegex(sqlite3.NotSupportedError, '100'):
            self.connection.execute('SELECT ' + ','.join('?' for _ in range(101)), list(range(101)))
        self.assertEqual(len(self.sql.statements), previous)

    def test_long_thai_literal_search_works_with_native_50_byte_like_limit(self):
        name = 'ชื่อพนักงานทดสอบ' * 8
        self.connection.execute('INSERT INTO people (name) VALUES (?)', (name,))
        self.sql.database.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 50)
        pattern = '%' + name[:40] + '%'
        rows = self.connection.execute('SELECT id FROM people WHERE name LIKE ? OR name LIKE ?', (pattern, pattern)).fetchall()
        self.assertEqual([tuple(row) for row in rows], [(1,)])
        self.assertIn('instr(lower(name)', self.sql.statements[-1])

    def test_long_like_and_large_in_list_share_correct_parameter_mapping(self):
        name = 'A' * 70
        self.connection.execute('INSERT INTO people (name) VALUES (?)', (name,))
        self.sql.database.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 50)
        placeholders = ','.join('?' for _ in range(500))
        statement = "SELECT id, '?' FROM people p WHERE p.name LIKE ? AND id IN (" + placeholders + ')'
        parameters = ['%' + 'a' * 60 + '%', *range(1, 501)]
        self.assertEqual([tuple(row) for row in self.connection.execute(statement, parameters)], [(1, '?')])
        self.assertEqual(self.sql.parameter_counts[-1], 2)

    def test_long_wildcard_search_gives_a_friendly_validation_error(self):
        self.sql.database.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, 50)
        with self.assertRaisesRegex(ValueError, 'shorter|wildcard'):
            self.connection.execute('SELECT id FROM people WHERE name LIKE ?', ('%' + 'x' * 60 + '_%',))

    def test_short_like_patterns_keep_their_original_wildcard_semantics(self):
        self.connection.execute('INSERT INTO people (name) VALUES (?)', ('Ada',))
        statement = 'SELECT id FROM people WHERE name LIKE ?'
        self.assertEqual([tuple(row) for row in self.connection.execute(statement, ('A_a',))], [(1,)])
        self.assertEqual(self.sql.statements[-1], statement)

    def test_long_like_with_an_expression_is_not_rewritten_as_a_partial_column(self):
        with self.assertRaisesRegex(ValueError, 'shorter'):
            self.connection.execute("SELECT id FROM people WHERE 'prefix' || name LIKE ?", ('%' + 'x' * 60 + '%',))

    def test_json_membership_keeps_values_as_data(self):
        name = "x'); DELETE FROM people; --"
        self.connection.execute('INSERT INTO people (name) VALUES (?)', (name,))
        names = [name, *[f'person-{i}' for i in range(500)]]
        statement = 'SELECT name FROM people WHERE name IN (' + ','.join('?' for _ in names) + ')'
        self.assertEqual([tuple(row) for row in self.connection.execute(statement, names)], [(name,)])
        self.assertEqual(self.connection.execute('SELECT COUNT(*) FROM people').fetchone()[0], 1)

    def test_json_membership_preserves_mixed_scalar_affinity_for_text_columns(self):
        names = ['1', '1.0', '001', '01', '1.00', '1e0', '2']
        self.connection.executemany('INSERT INTO people (name) VALUES (?)', [(name,) for name in names])
        parameters = [1, 1.0, '001', *[f'filler-{index}' for index in range(100)]]
        statement = 'SELECT name FROM people WHERE name IN (' + ','.join('?' for _ in parameters) + ') ORDER BY name'
        expected = self.sql.database.execute(statement, parameters).fetchall()
        self.assertEqual(expected, [('001',), ('1',), ('1.0',)])
        self.assertEqual([tuple(row) for row in self.connection.execute(statement, parameters)], expected)

    def test_javascript_null_is_normalized_before_json_serialization(self):
        sentinel = object()
        with patch.object(adapter, '_JS_NULL', sentinel, create=True):
            class NullSQL(FakeSQL):
                def exec(self, statement, *parameters):
                    result = super().exec(statement, *parameters)
                    result.rows = [tuple(sentinel if value is None else value for value in row) for row in result.rows]
                    return result
            sql = NullSQL()
            self.addCleanup(sql.database.close)
            for converter in (None, lambda value: value):
                connection = SQLiteConnection(sql, from_js=converter)
                connection.row_factory = sqlite3.Row
                row = connection.execute('SELECT NULL AS missing, 0 AS zero, 1 AS present').fetchone()
                self.assertIsNone(row['missing'])
                self.assertEqual(json.loads(json.dumps(dict(row))), {'missing': None, 'zero': 0, 'present': 1})

    def test_proxy_conversion_returning_javascript_null_is_normalized(self):
        sentinel = object()
        class Proxy:
            def to_py(self):
                return sentinel
        with patch.object(adapter, '_JS_NULL', sentinel, create=True):
            self.assertIsNone(adapter._plain(Proxy()))

    def test_all_excel_reports_accept_javascript_null_database_fields(self):
        import app
        import openpyxl
        sentinel = object()
        class NullSQL(FakeSQL):
            def exec(self, statement, *parameters):
                result = super().exec(statement, *parameters)
                result.rows = [tuple(sentinel if value is None else value for value in row) for row in result.rows]
                return result
        sql = NullSQL()
        self.addCleanup(sql.database.close)
        state = TransactionState()
        @contextmanager
        def connection():
            db = SQLiteConnection(sql, state)
            db.row_factory = sqlite3.Row
            try:
                yield db
            finally:
                db.close()
        with patch.object(adapter, '_JS_NULL', sentinel, create=True), patch.object(app, 'db_connection', connection):
            app.init_db()
            app.import_employees([('1', 'Test Employee', 'IT')], 'test-admin')
            output = app.xlsx_report_bytes('all')
        workbook = openpyxl.load_workbook(io.BytesIO(output), read_only=True)
        self.addCleanup(workbook.close)
        self.assertEqual(len(workbook.sheetnames), 9)
        self.assertIsNone(workbook['Wristbands']['F2'].value)


if __name__ == '__main__':
    unittest.main()
