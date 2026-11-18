"""Native SQL Server integration tests, explicitly opted in against LOCAL SQL only.

Set EVENTDESK_SQLSERVER_NATIVE_TESTS=1. The suite creates its own uniquely named
database on .\\SQLEXPRESS; it never connects to Employee_db or the photo server.
"""
from contextlib import contextmanager
import os
import re
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import uuid


ENABLED = os.environ.get('EVENTDESK_SQLSERVER_NATIVE_TESTS') == '1'


@unittest.skipUnless(ENABLED, 'Set EVENTDESK_SQLSERVER_NATIVE_TESTS=1 for the isolated local SQL suite.')
class NativeSqlServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import pyodbc
        from eventdesk_storage import initialize_schema
        cls.database = 'EventDeskMigrationTest_native_' + uuid.uuid4().hex[:12]
        cls.master = ('DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\\SQLEXPRESS;'
                      'DATABASE=master;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes')
        with pyodbc.connect(cls.master, autocommit=True) as master:
            master.execute('CREATE DATABASE [' + cls.database + ']')
            master.execute('ALTER DATABASE [' + cls.database + '] SET COMPATIBILITY_LEVEL=120')
        cls.connection_string = cls.master.replace('DATABASE=master;', 'DATABASE=' + cls.database + ';')
        with pyodbc.connect(cls.connection_string, autocommit=True) as raw:
            initialize_schema(raw)

    @classmethod
    def tearDownClass(cls):
        import pyodbc
        # An exact own-test-name guard precedes the destructive database operation.
        if not re.fullmatch(r'EventDeskMigrationTest_native_[0-9a-f]{12}', cls.database):
            raise RuntimeError('Unexpected test database name; refusing cleanup.')
        with pyodbc.connect(cls.master, autocommit=True) as master:
            master.execute('ALTER DATABASE [' + cls.database + '] SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
            master.execute('DROP DATABASE [' + cls.database + ']')

    def setUp(self):
        import pyodbc
        import app
        from eventdesk_storage import SqlServerConnection, TABLES
        with pyodbc.connect(self.connection_string, autocommit=True) as raw:
            for table in TABLES:
                raw.execute(f'ALTER TABLE [eventdesk].[{table}] NOCHECK CONSTRAINT ALL')
            for table in TABLES:
                raw.execute(f'DELETE FROM [eventdesk].[{table}]')
            for table in TABLES:
                raw.execute(f'ALTER TABLE [eventdesk].[{table}] WITH CHECK CHECK CONSTRAINT ALL')
        @contextmanager
        def connection():
            db = SqlServerConnection(self.connection_string)
            try:
                yield db
            finally:
                db.close()
        self.connection = connection
        self.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        self.addCleanup(self.temp.cleanup)
        self.path_patch = patch.object(app, 'DB_PATH', Path(self.temp.name) / 'unused-sqlite-fallback.db')
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.patch = patch.object(app, 'db_connection', connection)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        app.init_db()

    def test_unicode_identity_and_sqlite_binding_contract(self):
        import app
        app.import_employees([('12345', '\u0e2a\u0e21\u0e0a\u0e32\u0e22', 'IT')], 'test')
        with self.connection() as db:
            person = db.execute('SELECT * FROM employees').fetchone()
            self.assertEqual(person['employee_id'], '0012345')
            self.assertEqual(person['name'], '\u0e2a\u0e21\u0e0a\u0e32\u0e22')
            self.assertEqual(person['id'], person[0])
            self.assertEqual(app.find_employee_by_external_id(db, '00012345')['id'], person['id'])

    def test_distinct_departments_keep_case_sensitive_values_and_nocase_order(self):
        import app
        app.import_employees([('A1', 'One', 'IT'), ('A2', 'Two', 'it'), ('A3', 'Three', 'QA')], 'test')
        with self.connection() as db:
            rows = db.execute("SELECT DISTINCT department FROM employees WHERE "
                "TRIM(COALESCE(department,''))<>'' ORDER BY department COLLATE NOCASE").fetchall()
        self.assertEqual(sorted(row[0] for row in rows), ['IT', 'QA', 'it'])
        self.assertEqual([row[0].lower() for row in rows], ['it', 'it', 'qa'])

    def test_reward_case_accepts_integer_conditions_and_boolean_branches(self):
        with self.connection() as db:
            for changed, image in ((0, None), (1, None), (1, b'photo')):
                db.execute('INSERT INTO reward_drafts(name,quantity,action,image_changed,image) VALUES(?,?,?,?,?)',
                           ('Test', 1, 'save', changed, image))
            rows = db.execute('''SELECT CASE WHEN d.image_changed THEN d.image IS NOT NULL ELSE
                EXISTS(SELECT 1 FROM reward_images i WHERE i.prize_id=d.source_id) END AS has_image
                FROM reward_drafts d ORDER BY d.id''').fetchall()
            self.assertEqual([row[0] for row in rows], [0, 0, 1])
            row = db.execute('SELECT CASE WHEN ? THEN ?=1 ELSE ?=1 END AS value '
                             'FROM reward_drafts ORDER BY id LIMIT 1', (1, None, 0)).fetchone()
            self.assertIsNone(row[0])
            row = db.execute('SELECT CASE image_changed WHEN 1 THEN 7 ELSE 8 END AS value '
                             'FROM reward_drafts ORDER BY id LIMIT 1').fetchone()
            self.assertEqual(row[0], 8)
            row = db.execute('SELECT (NULL=1) AS value,SUM(source_id=1) AS total FROM reward_drafts').fetchone()
            self.assertEqual(tuple(row), (None, None))
            row = db.execute('SELECT CASE WHEN name LIKE ? THEN name LIKE ? ELSE 0 END AS value '
                             'FROM reward_drafts ORDER BY id LIMIT 1', ('Test%', 'Test%')).fetchone()
            self.assertEqual(row[0], 1)

    def test_distinct_text_roundtrip_preserves_null_spaces_and_supplementary_unicode(self):
        values = ['IT', 'it', 'IT ', 'IT\x00', '\x00', None, '',
                  '\u0e44\u0e17\u0e22\U0001f600', '\u54e1\u5de5\U00020000', 'IT']
        with self.connection() as db:
            for index, value in enumerate(values):
                db.execute('INSERT INTO employees(employee_id,name,checked_in_at,qr_token) VALUES(?,?,?,?)',
                           (f'BINARY-{index}', 'Binary Test', value, f'binary-qr-{index}'))
            rows = db.execute('SELECT DISTINCT checked_in_at AS dept FROM employees '
                              'ORDER BY dept COLLATE NOCASE').fetchall()
            self.assertCountEqual([row['dept'] for row in rows], list(dict.fromkeys(values)))
            self.assertTrue(all(row['dept'] is None or isinstance(row['dept'], str) for row in rows))

    def test_unicode_employee_id_and_literal_search_are_preserved(self):
        import app
        app.import_employees([('\u54e1\u5de5001', '[AB] Person', 'IT'), ('A002', 'Alice', 'IT')], 'test')
        with self.connection() as db:
            self.assertEqual(db.raw.timeout, 15)
            self.assertIsNotNone(db.execute('SELECT id FROM employees WHERE employee_id=?', ('\u54e1\u5de5001',)).fetchone())
            found = db.execute('SELECT name FROM employees WHERE name LIKE ?', ('[AB]%',)).fetchall()
            self.assertEqual([row[0] for row in found], ['[AB] Person'])
            found = db.execute("SELECT name FROM employees WHERE name LIKE ? ESCAPE '\\'", ('[AB]%',)).fetchall()
            self.assertEqual([row[0] for row in found], ['[AB] Person'])

    def test_delete_delta_is_a_primary_key_tombstone(self):
        import app
        app.import_employees([('E01', 'One', 'IT')], 'test')
        with self.connection() as db:
            db.drain_changes()
            row = db.execute('SELECT id FROM audit_logs').fetchone()
            db.execute('DELETE FROM audit_logs WHERE id=?', (row[0],))
            changes = db.drain_changes()
            self.assertEqual(changes, [{'table': 'audit_logs', 'key': {'id': row[0]}, 'action': 'delete', 'row': None}])

    def test_five_thousand_in_bindings_match_sqlite_selection(self):
        import app
        app.import_employees([('\u54e1\u5de5001', 'One', 'IT'), ('__ED_ARRAY_0__', 'Two', 'QA')], 'test')
        ids = ['missing-' + str(number) for number in range(4998)] + ['\u54e1\u5de5001', '__ED_ARRAY_0__']
        with self.connection() as db:
            query = 'SELECT employee_id FROM employees WHERE department=? AND employee_id IN (' + ','.join('?' for _ in ids) + ')'
            found = db.execute(query, ('IT', *ids)).fetchall()
            self.assertEqual([row[0] for row in found], ['\u54e1\u5de5001'])
            first, second = ids[:2500], ids[2500:]
            query = ('SELECT employee_id FROM employees WHERE department=? AND (employee_id IN ('
                     + ','.join('?' for _ in first) + ') OR employee_id IN ('
                     + ','.join('?' for _ in second) + '))')
            found = db.execute(query, ('QA', *first, *second)).fetchall()
            self.assertEqual([row[0] for row in found], ['__ED_ARRAY_0__'])

    def test_sql_2014_compatibility_and_xml_value_round_trip(self):
        from eventdesk_storage.compiler import compile_sql
        special = 'ID<>&\"\'\r\n\t\U00020000__ED_ARRAY_0__'
        with self.connection() as db:
            self.assertEqual(db.raw.execute('SELECT compatibility_level FROM sys.databases WHERE database_id=DB_ID()').fetchone()[0], 120)
            db.execute('INSERT INTO employees(employee_id,name,qr_token) VALUES (?,?,?)',
                       (special, '  Padded name  ', 'xml-test-qr'))
            db.execute('INSERT INTO employees(employee_id,name,qr_token) VALUES (?,?,?)',
                       ('', 'Empty ID', 'empty-xml-test-qr'))
            ids = ['missing-' + str(index) for index in range(2000)] + [special, '', None]
            query = 'SELECT employee_id,TRIM(name) AS name FROM employees WHERE employee_id IN (' + ','.join('?' for _ in ids) + ')'
            found = {row['employee_id']: row['name'] for row in db.execute(query, ids)}
            self.assertEqual(found, {special: 'Padded name', '': 'Empty ID'})
            query = 'SELECT id FROM employees WHERE employee_id NOT IN (' + ','.join('?' for _ in ids) + ')'
            self.assertEqual(db.execute(query, ids).fetchall(), [])
            actual_ids = [row[0] for row in db.execute('SELECT id FROM employees')]
            integers = [None, -7, -(2**63), 2**63 - 1, *range(2000), *actual_ids]
            query = 'SELECT id FROM employees WHERE id IN (' + ','.join('?' for _ in integers) + ')'
            compiled = compile_sql(query, integers)
            self.assertEqual(len(db.execute(query, integers).fetchall()), 2)
            self.assertNotIn('OPENJSON', compiled.sql)

    def test_upsert_and_nullable_unique_indexes(self):
        import app
        with self.connection() as db:
            app.set_setting(db, 'test', {'a': 1}, 'test')
            app.set_setting(db, 'test', {'a': 2}, 'test')
            self.assertEqual(app.get_setting('test', db=db), {'a': 2})
        app.import_employees([('E01', 'One', 'IT'), ('E02', 'Two', 'IT')], 'test')
        with self.connection() as db:
            tokens = [row[0] for row in db.execute('SELECT qr_token FROM employees ORDER BY id')]
        for token in tokens:
            app.scan_action(token, 'checkin', 'test')
        with self.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM event_checkins').fetchone()[0], 2)

    def test_managed_rollback_and_final_row_delta(self):
        import pyodbc
        from eventdesk_storage import SqlServerConnection
        raw = pyodbc.connect(self.connection_string, autocommit=False)
        try:
            db = SqlServerConnection(raw_connection=raw, managed=True)
            db.execute('BEGIN IMMEDIATE')
            cursor = db.execute("INSERT INTO employees(employee_id,name,department,qr_token) VALUES (?,?,?,?)",
                                ('A01', 'Before', 'IT', 'test-qr-token'))
            db.execute('UPDATE employees SET name=? WHERE id=?', ('After', cursor.lastrowid))
            db.commit()
            changes = db.drain_changes()
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]['row']['name'], 'After')
            db.rollback()
            self.assertTrue(db.rollback_requested)
            db.final_rollback()
            self.assertEqual(db.execute('SELECT COUNT(*) FROM employees').fetchone()[0], 0)
        finally:
            raw.close()


if ENABLED:
    # Re-run the existing business acceptance tests on the real native provider.
    # SQLite's legacy-file migration test remains in the original SQLite suite.
    import tests.test_app as _legacy_tests
    import tests.test_manual_employee as _manual_tests
    for _source in (_legacy_tests.BusinessRulesTests, _legacy_tests.UpgradeAcceptanceTests,
                    _manual_tests.ManualEmployeeTests):
        for _name, _method in vars(_source).items():
            if (_name.startswith('test_') and 'additive_migration' not in _name and 'hosted_sqlite_adapter' not in _name
                    or _name in ('active_token', 'inspect_and_preview', 'configured_draw')):
                setattr(NativeSqlServerTests, _name, _method)
    del _source, _name, _method


if __name__ == '__main__':
    unittest.main()
