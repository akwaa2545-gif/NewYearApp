"""Opt-in rehearsal using a private export and an owned LOCAL test database.

Enable EVENTDESK_SQLSERVER_REHEARSAL_TESTS=1. No connection string override is
accepted: every SQL write targets a newly created .\\SQLEXPRESS database. Real
employees are copied for integrity checks; all new attendance uses synthetic IDs.
"""
import base64
from contextlib import closing
import copy
import json
import os
from pathlib import Path
import re
import secrets
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
import uuid

import app
from cloudflare.event_primary import PrimaryUnavailable, SQLPrimary
from cloudflare.handler_adapter import dispatch
from cloudflare.replica import ReplicaStore
from eventdesk_storage import TABLES, initialize_schema
from photo_api.event_backend import EventEngine
from photo_api.event_protocol import encode_response, parse_command
from photo_api.event_store import SQLiteStore, SqlServerStore
from scripts.migrate_sql_event import load_source, migrate, summarize


SOURCE = app.ROOT / 'data' / 'sql-migration' / 'live-rehearsal-export.normalized.json'
ENABLED = os.environ.get('EVENTDESK_SQLSERVER_REHEARSAL_TESTS') == '1'
ORIGIN = 'https://eventdesk.rehearsal.test'


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def _cleanup_folder(folder):
    # Windows scanners can briefly hold an otherwise unused temporary directory.
    for attempt in range(10):
        try:
            folder.cleanup()
            return
        except OSError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32) or attempt == 9:
                raise
            time.sleep(0.1)


@unittest.skipUnless(ENABLED, 'Enable the private LOCAL SQL migration rehearsal explicitly.')
class SQLMigrationRehearsalTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_file():
            raise unittest.SkipTest('The private normalized rehearsal export is unavailable.')
        import pyodbc
        cls.source = load_source(SOURCE)
        cls.source_manifest = summarize(cls.source)
        cls.database = 'EventDeskMigrationTest_rehearsal_' + uuid.uuid4().hex[:12]
        cls.master = ('DRIVER={ODBC Driver 18 for SQL Server};SERVER=.\\SQLEXPRESS;'
                      'DATABASE=master;Trusted_Connection=yes;Encrypt=yes;TrustServerCertificate=yes')
        with closing(pyodbc.connect(cls.master, autocommit=True)) as raw:
            raw.execute('CREATE DATABASE [' + cls.database + ']')
            cls.addClassCleanup(cls.drop_database)
            raw.execute('ALTER DATABASE [' + cls.database + '] SET COMPATIBILITY_LEVEL=120')
        cls.connection_string = cls.master.replace('DATABASE=master;', 'DATABASE=' + cls.database + ';')
        with closing(pyodbc.connect(cls.connection_string, autocommit=True)) as raw:
            initialize_schema(raw, 'dbo')
        cls.import_report = migrate(cls.source, cls.connection_string, schema='dbo', commit=True)
        cls.sql_store = SqlServerStore(cls.connection_string, schema='dbo')
        with cls.sql_store.transaction() as tx:
            actual = summarize({'tables': tx.all_rows()})
        if actual != cls.source_manifest:
            raise AssertionError('The isolated SQL clone does not match the complete export hashes/counts.')

    @classmethod
    def drop_database(cls):
        import pyodbc
        if not re.fullmatch(r'EventDeskMigrationTest_rehearsal_[0-9a-f]{12}', cls.database):
            raise RuntimeError('Unexpected test database name; refusing cleanup.')
        with closing(pyodbc.connect(cls.master, autocommit=True)) as raw:
            raw.execute('ALTER DATABASE [' + cls.database + '] SET SINGLE_USER WITH ROLLBACK IMMEDIATE')
            raw.execute('DROP DATABASE [' + cls.database + ']')

    async def asyncSetUp(self):
        self.folder = TemporaryDirectory(dir=app.ROOT / 'data')
        self.addCleanup(_cleanup_folder, self.folder)
        self.mirror_path = Path(self.folder.name) / 'fallback.sqlite3'
        self.password = secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        credential = {'salt': salt, 'hash': app.password_hash(self.password, salt)}
        self.config = {'event_name': 'Isolated SQL Rehearsal', 'secret': secrets.token_hex(32),
                       'admin_password': credential, 'staff_password': credential}
        self.engine = EventEngine(self.sql_store, app, self.config, ORIGIN)
        self.cookie, self.csrf = None, None
        login = self.engine.handle(self.command('/api/login', data={'password': self.password, 'admin_only': True}))
        self.assertEqual(login['status'], 200)
        self.cookie = next(value.split(';')[0] for name, value in login['headers'] if name.lower() == 'set-cookie')
        self.csrf = self.body(login)['csrf']
        self.employee_id = 'REHEARSAL_' + uuid.uuid4().hex[:16]
        created = self.engine.handle(self.command('/api/admin/employees', data={
            'employee_id': self.employee_id, 'name': 'Synthetic Rehearsal Employee',
            'department': 'Rehearsal', 'position': 'ENGINEER', 'eligible_for_draw': True}))
        self.assertEqual(created['status'], 201)
        with self.sql_store.transaction() as tx:
            self.qr_code = tx.db.execute('SELECT qr_token FROM employees WHERE employee_id=?',
                                         (self.employee_id,)).fetchone()[0]
        with patch.object(app, 'DB_PATH', self.mirror_path):
            app.init_db()
        self.fallback_store = SQLiteStore(self.mirror_path)
        self.fallback_engine = EventEngine(self.fallback_store, app, self.config, ORIGIN)
        self.atomic(lambda store: store.initialize())
        self.mode, self.loss_operation = 'healthy', None
        self.calls, self.fallback_calls = [], []
        self.router = SQLPrimary(self.transport, self.atomic, self.fallback, TABLES, self.config['secret'])
        await self.router.bootstrap()
        self.assertEqual(self.atomic(lambda store: store.state())['ready'], True)

    def command(self, path='/api/stats', method='POST', data=None, *, operation_id=None):
        headers = [['Content-Type', 'application/json'], ['Origin', ORIGIN]]
        if self.cookie:
            headers.extend([['Cookie', self.cookie], ['X-CSRF-Token', self.csrf]])
        return {'schema_version': 1, 'operation_id': operation_id or str(uuid.uuid4()),
                'method': method, 'path': path, 'headers': headers,
                'body_base64': '' if method == 'GET' else base64.b64encode(_json(data or {})).decode(),
                'client_ip': '127.0.0.1', 'after_revision': 0}

    @staticmethod
    def body(response):
        raw = base64.b64decode(response['body_base64']) if 'body_base64' in response else response['body']
        return json.loads(raw)

    @staticmethod
    def headers(response):
        return {name.lower(): value for name, value in response['headers']}

    def atomic(self, action):
        with self.fallback_store.transaction() as tx:
            return action(ReplicaStore(tx.db, TABLES))

    async def transport(self, kind, envelope):
        self.calls.append((kind, copy.deepcopy(envelope)))
        if self.mode == 'down':
            raise PrimaryUnavailable('Synthetic transport outage.')
        result = self.engine.snapshot(envelope) if kind == 'snapshot' else self.engine.handle(envelope)
        if kind == 'request' and self.mode == 'lose-ack' and envelope['operation_id'] == self.loss_operation:
            self.mode = 'down'
            raise PrimaryUnavailable('Synthetic response lost after SQL commit.')
        return result

    async def fallback(self, envelope, kind):
        self.fallback_calls.append((kind, envelope['operation_id']))
        parsed = parse_command(envelope)
        with self.fallback_store.transaction() as tx, self.fallback_engine._bind(
                tx, parsed.recorded_at if kind == 'checkin' else None):
            result = dispatch(app, parsed.method, parsed.path,
                              [*parsed.headers, ('Host', 'eventdesk.rehearsal.test')], parsed.body,
                              parsed.client_ip, self.config)
            if result['status'] >= 400 or tx.db.rollback_requested:
                raise AssertionError('A rehearsal fallback application request was rejected.')
            if kind:
                ReplicaStore(tx.db, TABLES).enqueue(envelope, kind, encode_response(result))
            return result

    def sql_checkins(self):
        with self.sql_store.transaction() as tx:
            return tx.db.execute('''SELECT c.id,c.checked_in_at FROM event_checkins c JOIN employees e
                ON e.id=c.employee_id WHERE e.employee_id=?''', (self.employee_id,)).fetchall()

    def snapshot_digest(self, tables):
        return summarize({'tables': tables})['source_sha256']

    async def test_live_clone_integrity_and_repeat_import_preserve_current_data(self):
        self.assertEqual(len(self.source_manifest['counts']), 26)
        self.assertEqual(self.source_manifest['counts']['employees'], 1709)
        self.assertEqual(self.import_report['source_sha256'], self.source_manifest['source_sha256'])
        with self.sql_store.transaction() as tx:
            before = self.snapshot_digest(tx.all_rows())
        repeated = migrate(self.source, self.connection_string, schema='dbo', commit=True)
        self.assertTrue(repeated['already_imported'])
        with self.sql_store.transaction() as tx:
            self.assertEqual(self.snapshot_digest(tx.all_rows()), before)
        # Every original internal ID, displayed ID, and QR token was covered by
        # the full-table digest before any synthetic writes in setUpClass.
        for table in ('employees', 'wristbands', 'reward_images', '_eventdesk_projector_images'):
            self.assertIn(table, self.source_manifest['table_sha256'])

    async def test_real_admin_and_legacy_staff_read_routes(self):
        admin_only = ('/api/employees', '/api/wristbands', '/api/import/history', '/api/settings',
                      '/api/users', '/api/admin/draw-history', '/api/admin/activity',
                      '/api/export/attendance', '/api/export/winners', '/api/export/qr',
                      '/api/export/report?type=all', '/api/wristbands/qr?employee_id=' + self.employee_id)
        common = ('/api/me', '/api/stats', '/api/attendance', '/api/draw/config',
                  '/api/draw/eligible-ids', '/api/rewards', '/api/projector/state',
                  '/api/projector/events', '/api/winners', '/api/verifications')
        employee_filters = tuple('/api/employees?' + query for query in (
            'status=eligible', 'status=checkedin&sort=checkin_desc', 'status=verified',
            'status=pending', 'status=verification_failed', 'status=lucky_participant',
            'status=winner', 'q=Rehearsal&sort=department&page=2', 'wristband=issued',
            'department=Rehearsal&sort=name'))
        for path in (*admin_only, *common, *employee_filters):
            with self.subTest(role='admin', path=path):
                self.assertEqual(self.engine.handle(self.command(path, 'GET'))['status'], 200)
        staff = self.engine.handle(self.command('/api/staff-session'))
        self.assertEqual(staff['status'], 200)
        self.assertEqual(self.body(staff)['role'], 'staff')
        self.cookie = next(value.split(';')[0] for name, value in staff['headers'] if name.lower() == 'set-cookie')
        self.csrf = self.body(staff)['csrf']
        for path in (*common, '/api/attendance?status=checked', '/api/attendance?status=pending',
                     '/api/attendance?status=lost', '/api/attendance?status=unreadable',
                     '/api/attendance?employee_id=REHEARSAL', '/api/verifications?employee_id=' + self.employee_id):
            with self.subTest(role='staff', path=path):
                self.assertEqual(self.engine.handle(self.command(path, 'GET'))['status'], 200)
        for path in admin_only:
            with self.subTest(role='staff-denied', path=path):
                self.assertEqual(self.engine.handle(self.command(path, 'GET'))['status'], 403)

    async def test_sql_to_offline_checkin_and_recovery_is_exactly_once(self):
        healthy = await self.router.handle(self.command('/api/stats', 'GET'))
        self.assertEqual(healthy['status'], 200)
        self.assertEqual(self.headers(healthy)['x-eventdesk-storage'], 'sql')
        self.assertEqual(self.fallback_calls, [])
        self.mode = 'down'
        checkin = self.command('/api/scan/checkin', data={'code': self.qr_code})
        offline = await self.router.handle(checkin)
        duplicate = await self.router.handle(checkin)
        self.assertEqual((offline['status'], duplicate['status']), (202, 202))
        self.assertTrue(self.body(offline)['pending_sync'])
        self.assertEqual(len(self.atomic(lambda store: store.pending())), 1)
        self.assertEqual(self.sql_checkins(), [])
        self.mode = 'healthy'
        recovered = await self.router.handle(self.command('/api/stats', 'GET'))
        self.assertEqual(recovered['status'], 200)
        self.assertEqual(self.headers(recovered)['x-eventdesk-storage'], 'sql')
        self.assertEqual(self.atomic(lambda store: store.pending()), [])
        rows = self.sql_checkins()
        self.assertEqual(len(rows), 1)
        confirmed = await self.router.handle(checkin)
        self.assertEqual(confirmed['status'], 200)
        self.assertEqual(len(self.sql_checkins()), 1)
        with self.sql_store.transaction() as tx:
            self.assertIsNotNone(tx.receipt(checkin['operation_id']))
        with self.fallback_store.transaction() as tx:
            row = tx.db.execute('''SELECT c.checked_in_at FROM event_checkins c JOIN employees e
                ON e.id=c.employee_id WHERE e.employee_id=?''', (self.employee_id,)).fetchone()
            self.assertEqual(row[0], rows[0]['checked_in_at'])

    async def test_unknown_sql_commit_recovers_its_original_receipt(self):
        checkin = self.command('/api/scan/checkin', data={'code': self.qr_code})
        self.loss_operation, self.mode = checkin['operation_id'], 'lose-ack'
        pending = await self.router.handle(checkin)
        self.assertEqual(pending['status'], 202)
        original = self.sql_checkins()
        self.assertEqual(len(original), 1)
        with self.sql_store.transaction() as tx:
            original_receipt = tx.receipt(checkin['operation_id'])
        self.assertIsNotNone(original_receipt)
        self.assertEqual(len(self.atomic(lambda store: store.pending())), 1)
        self.mode = 'healthy'
        result = await self.router.handle(self.command('/api/stats', 'GET'))
        self.assertEqual(result['status'], 200)
        self.assertEqual(self.atomic(lambda store: store.pending()), [])
        current = self.sql_checkins()
        self.assertEqual([(row['id'], row['checked_in_at']) for row in current],
                         [(row['id'], row['checked_in_at']) for row in original])
        with self.sql_store.transaction() as tx:
            self.assertEqual(tx.receipt(checkin['operation_id']), original_receipt)
        receipt = self.engine.handle({**checkin, 'receipt_only': True})
        self.assertTrue(receipt['receipt_found'])
        self.assertEqual(receipt['status'], original_receipt['response']['status'])
        self.assertEqual(receipt['body_base64'], original_receipt['response']['body_base64'])

    async def test_unexecuted_draw_is_cancelled_before_late_delivery(self):
        checked = await self.router.handle(self.command('/api/scan/checkin', data={'code': self.qr_code}))
        self.assertEqual(checked['status'], 200)
        prize = self.engine.handle(self.command('/api/prizes', data={
            'name': 'Synthetic Rehearsal Prize ' + uuid.uuid4().hex[:8], 'quantity': 1}))
        self.assertEqual(prize['status'], 200)
        prize_id = self.body(prize)['id']
        # Restrict the isolated clone's draw to the synthetic employee. This
        # never changes any real employee's attendance, either locally or live.
        with self.sql_store.transaction() as tx, self.engine._bind(tx):
            tx.db.execute('UPDATE lucky_draw_participants SET active=0 WHERE employee_id NOT IN '
                          '(SELECT id FROM employees WHERE employee_id=?)', (self.employee_id,))
            app.set_setting(tx.db, 'require_verification_for_draw', False, 'rehearsal')
            app.set_setting(tx.db, 'allow_multiple_wins', False, 'rehearsal')
            tx.finish(None, None, {})
            before = tx.db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0]
        await self.router.handle(self.command('/api/stats', 'GET'))
        draw = self.command('/api/draw', data={'prize_id': prize_id, 'round_name': 'Synthetic rehearsal'})
        self.mode = 'down'
        blocked = await self.router.handle(draw)
        self.assertEqual(blocked['status'], 503)
        self.assertEqual(len(self.atomic(lambda store: store.pending())), 1)
        self.mode = 'healthy'
        await self.router.handle(self.command('/api/stats', 'GET'))
        self.assertEqual(self.atomic(lambda store: store.pending()), [])
        late = self.engine.handle({**draw, 'after_revision': 0})
        self.assertEqual(late['status'], 409)
        self.assertTrue(late['receipt_found'])
        self.assertTrue(late['cancelled'])
        with self.sql_store.transaction() as tx:
            self.assertEqual(tx.db.execute('SELECT COUNT(*) FROM lucky_draw_winners').fetchone()[0], before)


if __name__ == '__main__':
    unittest.main()
