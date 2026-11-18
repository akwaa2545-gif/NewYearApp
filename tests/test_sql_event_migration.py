"""Complete-source validation and isolated native migration checks."""
import base64
from contextlib import closing
from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
import uuid
from unittest.mock import patch

import app
from eventdesk_storage.schema import TABLES
from scripts.migrate_sql_event import MigrationError, load_source, main, migrate, summarize, validate_snapshot


class EventMigrationValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(dir=app.ROOT / 'data')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'source.sqlite3'
        with patch.object(app, 'DB_PATH', self.path):
            app.init_db()
            app.import_employees([('0000123', '\u54e1\u5de5\u6e2c\u8a66', 'IT')], 'test')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(TABLES['_eventdesk_projector_images']['sqlite_sql'])

    def test_sqlite_backup_and_json_have_the_same_digest(self):
        snapshot = load_source(self.path)
        summary = summarize(snapshot)
        self.assertEqual(summary['counts']['employees'], 1)
        wire = {'schema_version': 1, 'revision': 0, 'tables': snapshot['tables']}
        path = Path(self.temp.name) / 'export.json'
        path.write_text(json.dumps(wire), encoding='utf-8')
        self.assertEqual(summarize(load_source(path)), summary)
        self.assertNotIn('\u54e1\u5de5', json.dumps(summary))

    def test_partial_duplicate_and_bad_foreign_key_sources_are_rejected(self):
        source = load_source(self.path)
        partial = {**source, 'tables': {key: value for key, value in source['tables'].items() if key != 'audit_logs'}}
        with self.assertRaises(MigrationError):
            validate_snapshot(partial)
        duplicate = {**source, 'tables': {**source['tables'], 'employees': source['tables']['employees'] * 2}}
        with self.assertRaises(MigrationError):
            validate_snapshot(duplicate)
        wristband = {**source['tables']['wristbands'][0], 'employee_id': 9999}
        bad_fk = {**source, 'tables': {**source['tables'], 'wristbands': [wristband]}}
        with self.assertRaises(MigrationError):
            validate_snapshot(bad_fk)

    def test_chunked_images_are_decoded_and_checked(self):
        import hashlib
        payload = b'\x89PNG\r\n\x1a\n' + b'abc' * 200
        digest = hashlib.sha256(payload).hexdigest()
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('CREATE TABLE _eventdesk_blobs(blob_id TEXT,chunk_number INTEGER,data BLOB,PRIMARY KEY(blob_id,chunk_number))')
            db.execute('INSERT INTO _eventdesk_blobs VALUES(?,?,?)', (digest, 0, payload))
            db.execute('INSERT INTO _eventdesk_projector_images VALUES(?,?,?)',
                       (digest, 'image/png', b'\x00eventdesk-blob:v1:' + digest.encode('ascii')))
        source = load_source(self.path)
        self.assertEqual(source['tables']['_eventdesk_projector_images'][0]['image'], payload)
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute('DELETE FROM _eventdesk_blobs')
        with self.assertRaises(MigrationError):
            load_source(self.path)

    def test_dry_run_cli_never_loads_the_sql_driver_or_prints_employee_data(self):
        output = io.StringIO()
        with patch.dict('sys.modules', {'pyodbc': None}), redirect_stdout(output):
            result = main(['--source', str(self.path)])
        self.assertEqual(result, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report['mode'], 'dry-run')
        self.assertEqual(report['counts']['employees'], 1)
        self.assertNotIn('qr_token', output.getvalue())
        self.assertNotIn('\u54e1\u5de5', output.getvalue())

    def test_cli_refuses_unsafe_modes_and_invalid_target_schema(self):
        for args in (['--commit'], ['--commit', '--dry-run'], ['--schema', 'dbo.x']):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(['--source', str(self.path), *args]), 1)
                self.assertIn('error', json.loads(output.getvalue()))

    def test_json_binary_and_invalid_field_types_are_checked_before_sql(self):
        source = load_source(self.path)
        payload = b'\x89PNG\r\n\x1a\n' + b'private-test-image'
        image = {'image_id': 'a' * 64, 'content_type': 'image/png',
                 'image': {'$bytes': base64.b64encode(payload).decode('ascii')}}
        source = {**source, 'tables': {**source['tables'], '_eventdesk_projector_images': [image]}}
        path = Path(self.temp.name) / 'snapshot.json'
        path.write_text(json.dumps(source), encoding='utf-8')
        self.assertEqual(load_source(path)['tables']['_eventdesk_projector_images'][0]['image'], payload)
        invalid_image = {**image, 'image': {'$bytes': '@@invalid-base64'}}
        invalid = {**source, 'tables': {**source['tables'], '_eventdesk_projector_images': [invalid_image]}}
        with self.assertRaises(MigrationError):
            validate_snapshot(invalid)
        for key, value in (('id', 'not-an-integer'), ('id', None), ('name', None), ('name', '\ud800')):
            row = {**source['tables']['employees'][0], key: value}
            invalid = {**source, 'tables': {**source['tables'], 'employees': [row]}}
            with self.subTest(field=key, value=repr(value)), self.assertRaises(MigrationError):
                validate_snapshot(invalid)

    def test_legacy_boolean_requires_explicit_export_normalization(self):
        source = load_source(self.path)
        draft = {'id': 1, 'source_id': None, 'name': 'Test Draft', 'quantity': 1,
                 'action': 'save', 'content_type': None, 'image': None, 'image_changed': 'True'}
        legacy = {**source, 'tables': {**source['tables'], 'reward_drafts': [draft]}}
        with self.assertRaisesRegex(MigrationError, 'integer'):
            validate_snapshot(legacy)
        normalized = {**source, 'tables': {**source['tables'], 'reward_drafts': [{**draft, 'image_changed': 1}]}}
        self.assertEqual(validate_snapshot(normalized)['tables']['reward_drafts'][0]['image_changed'], 1)


@unittest.skipUnless(os.environ.get('EVENTDESK_SQLSERVER_NATIVE_TESTS') == '1',
                     'Set EVENTDESK_SQLSERVER_NATIVE_TESTS=1 for isolated SQL migration tests.')
class NativeEventMigrationTests(EventMigrationValidationTests):
    @classmethod
    def setUpClass(cls):
        import pyodbc
        cls.database = 'EventDeskMigrationTest_import_' + uuid.uuid4().hex[:12]
        cls.master = ('DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\\SQLEXPRESS;'
                      'DATABASE=master;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes')
        with closing(pyodbc.connect(cls.master, autocommit=True)) as master:
            master.execute('CREATE DATABASE [' + cls.database + ']')
            master.execute('ALTER DATABASE [' + cls.database + '] SET COMPATIBILITY_LEVEL=120')
        cls.sql_connection = cls.master.replace('DATABASE=master;', 'DATABASE=' + cls.database + ';')

    @classmethod
    def tearDownClass(cls):
        import pyodbc
        import re
        if not re.fullmatch(r'EventDeskMigrationTest_import_[0-9a-f]{12}', cls.database):
            raise RuntimeError('Unexpected test database; refusing cleanup.')
        with closing(pyodbc.connect(cls.master, autocommit=True)) as master:
            master.execute('ALTER DATABASE [' + cls.database + '] SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
            master.execute('DROP DATABASE [' + cls.database + ']')

    def setUp(self):
        super().setUp()
        import pyodbc
        from eventdesk_storage import initialize_schema
        self.schema = 'migration_' + uuid.uuid4().hex[:8]
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            initialize_schema(raw, self.schema)

    def source_with_employee_id(self, identity):
        with closing(sqlite3.connect(self.path)) as source, source:
            source.execute('PRAGMA foreign_keys=ON')
            source.execute('BEGIN IMMEDIATE')
            source.execute('PRAGMA defer_foreign_keys=ON')
            source.execute('UPDATE employees SET id=?', (identity,))
            for table, entry in TABLES.items():
                for foreign in entry['foreign_keys']:
                    if foreign[2] == 'employees':
                        source.execute('UPDATE "' + table + '" SET "' + foreign[3] + '"=? WHERE "' + foreign[3] + '"=1',
                                       (identity,))
        return load_source(self.path)

    def test_native_import_is_exact_idempotent_and_reseeds_identities(self):
        import pyodbc
        snapshot = self.source_with_employee_id(9001)
        result = migrate(snapshot, self.sql_connection, schema=self.schema, commit=True)
        self.assertFalse(result['already_imported'])
        self.assertEqual(result['source_sha256'], summarize(snapshot)['source_sha256'])
        repeated = migrate(snapshot, self.sql_connection, schema=self.schema, commit=True)
        self.assertTrue(repeated['already_imported'])
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            row = raw.execute(f'SELECT id,employee_id,name FROM [{self.schema}].[employees]').fetchone()
            self.assertEqual(tuple(row), (9001, '0000123', '\u54e1\u5de5\u6e2c\u8a66'))
            inserted = raw.execute(f'INSERT INTO [{self.schema}].[employees] (employee_id,name,qr_token) '
                'OUTPUT INSERTED.id VALUES (?,?,?)', ('N02', 'Next', 'next-test-qr')).fetchone()[0]
            self.assertEqual(inserted, 9002)

    def test_restricted_user_import_needs_no_dbcc_or_owner_role(self):
        import pyodbc
        snapshot = self.source_with_employee_id(42)
        username = 'migration_user_' + uuid.uuid4().hex[:12]
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            raw.execute(f'CREATE USER [{username}] WITHOUT LOGIN')
            raw.execute(f'GRANT CREATE TABLE TO [{username}]')
            raw.execute(f'GRANT ALTER,SELECT,INSERT,UPDATE,DELETE,REFERENCES ON SCHEMA::[{self.schema}] TO [{username}]')
        real_connect = pyodbc.connect
        class RestrictedConnection:
            def __init__(self, raw):
                self.raw = raw
            def __getattr__(self, name):
                return getattr(self.raw, name)
            @property
            def timeout(self):
                return self.raw.timeout
            @timeout.setter
            def timeout(self, value):
                self.raw.timeout = value
            def close(self):
                try:
                    if not self.raw.autocommit:
                        self.raw.rollback()
                    self.raw.execute('REVERT')
                    if not self.raw.autocommit:
                        self.raw.commit()
                finally:
                    self.raw.close()
        def restricted_connect(*args, **kwargs):
            raw = real_connect(*args, **kwargs)
            try:
                raw.execute(f"EXECUTE AS USER='{username}'")
                return RestrictedConnection(raw)
            except BaseException:
                raw.close()
                raise
        with closing(restricted_connect(self.sql_connection, autocommit=True)) as raw:
            permissions = raw.execute("SELECT IS_ROLEMEMBER('db_owner'),IS_ROLEMEMBER('db_ddladmin'),"
                                      "IS_SRVROLEMEMBER('sysadmin')").fetchone()
            self.assertEqual(tuple(permissions[:2]), (0, 0))
            self.assertNotEqual(permissions[2], 1)
            with self.assertRaises(pyodbc.Error) as denied:
                raw.execute(f"DBCC CHECKIDENT ('[{self.schema}].[employees]', RESEED, 0) WITH NO_INFOMSGS")
            self.assertIn('2557', str(denied.exception))
        with patch.object(pyodbc, 'connect', side_effect=restricted_connect):
            result = migrate(snapshot, self.sql_connection, schema=self.schema, commit=True)
        self.assertEqual(result['source_sha256'], summarize(snapshot)['source_sha256'])
        with closing(restricted_connect(self.sql_connection, autocommit=True)) as raw:
            current = raw.execute('SELECT CONVERT(BIGINT,IDENT_CURRENT(?))',
                                  (f'[{self.schema}].[employees]',)).fetchone()[0]
            self.assertGreaterEqual(current, 42)
            inserted = raw.execute(f'INSERT INTO [{self.schema}].[employees] (employee_id,name,qr_token) '
                'OUTPUT INSERTED.id VALUES (?,?,?)', ('AFTER42', 'Next', 'restricted-user-next-qr')).fetchone()[0]
            self.assertGreater(inserted, 42)

    def test_existing_rows_are_never_overwritten(self):
        import pyodbc
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            raw.execute(f'INSERT INTO [{self.schema}].[employees] (employee_id,name,qr_token) VALUES (?,?,?)',
                        ('EXISTING', 'Preserve', 'preserve-test-qr'))
        with self.assertRaisesRegex(MigrationError, 'not empty'):
            migrate(load_source(self.path), self.sql_connection, schema=self.schema, commit=True)
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            self.assertEqual(raw.execute(f'SELECT employee_id FROM [{self.schema}].[employees]').fetchone()[0], 'EXISTING')

    def test_native_failure_rolls_back_every_imported_table(self):
        import pyodbc
        source = load_source(self.path)
        employee = {**source['tables']['employees'][0], 'employee_id': 'x' * 129}
        source = {**source, 'tables': {**source['tables'], 'employees': [employee]}}
        with self.assertRaises(MigrationError):
            migrate(source, self.sql_connection, schema=self.schema, commit=True)
        with closing(pyodbc.connect(self.sql_connection, autocommit=True)) as raw:
            for table in TABLES:
                self.assertEqual(raw.execute(f'SELECT COUNT(*) FROM [{self.schema}].[{table}]').fetchone()[0], 0)
            self.assertEqual(raw.execute(f'SELECT COUNT(*) FROM [{self.schema}].[EventMigrationReceipt]').fetchone()[0], 0)

    def test_lost_commit_acknowledgment_reconciles_the_saved_receipt(self):
        import pyodbc
        snapshot = load_source(self.path)
        real_connect = pyodbc.connect
        closed = []
        class LostAcknowledgment:
            def __init__(self, raw):
                self.raw = raw
            def __getattr__(self, name):
                return getattr(self.raw, name)
            @property
            def timeout(self):
                return self.raw.timeout
            @timeout.setter
            def timeout(self, value):
                self.raw.timeout = value
            def commit(self):
                self.raw.commit()
                raise pyodbc.OperationalError('Synthetic lost commit acknowledgment.')
            def close(self):
                closed.append(True)
                self.raw.close()
        with patch.object(pyodbc, 'connect', side_effect=lambda *args, **kwargs: LostAcknowledgment(real_connect(*args, **kwargs))):
            with self.assertRaisesRegex(MigrationError, 'Retry the same source'):
                migrate(snapshot, self.sql_connection, schema=self.schema, commit=True)
        self.assertEqual(closed, [True])
        self.assertTrue(migrate(snapshot, self.sql_connection, schema=self.schema, commit=True)['already_imported'])


if __name__ == '__main__':
    unittest.main()
